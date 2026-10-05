"""PAPER_TRADING_V1.5.9 acceptance test: the new-position market-hours
gate must never suppress existing-position Lifecycle Engine/Risk
kill-switch monitoring. Exercises the REAL production entry point,
`scripts/run_validation_cycle.py`'s own `run_validation_cycle()` (and,
for item 10, the dashboard's `POST /api/validation-cycle/run` route),
never just a library function in isolation. Runs entirely offline
against temporary sqlite files (reusing the `environment` fixture from
`test_review_only_daily_cycle.py`), never `data/options_agent.db`,
never a live market-data provider, never the actual current clock.

Covers the 12 edge cases PAPER_TRADING_V1.5.9's own task spec requires:
 1. scan window OPEN + zero positions -> normal scanning preserved
 2. scan window CLOSED + zero positions -> safe no-op (byte-identical to
    pre-V1.5.9, proven by the untouched tests in test_market_hours_gate.py)
 3. scan window CLOSED + one position -> lifecycle path reached
 4. scan window CLOSED + multiple positions -> lifecycle evaluates all
 5. scan window CLOSED + position + stale data -> fails closed, observable
 6. scan window CLOSED + position + provider failure -> observable, no candidate
 7. scan window OPEN + position -> lifecycle still runs alongside scanning
 8. weekend/non-trading-day -> explicitly tested, not conflated with the gate
 9. idempotency -> repeated invocation never duplicates lifecycle records
10. dashboard-triggered cycle -> identical safety semantics to the CLI
11. no position/order can ever be created by the lifecycle-only path
12. (covered by the untouched, still-passing tests in
    test_market_hours_gate.py and test_review_only_daily_cycle.py --
    V1.5.8-era zero-position behavior is unchanged)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.market_calendar import EASTERN
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio.account_state import SqlitePortfolioStore
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from src.portfolio.persistence import SqliteControlLoopStore
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    NOW,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

_BLOCKED = ValidationCycleEligibility(
    as_of=datetime(2026, 9, 25, 13, 21, tzinfo=timezone.utc),
    market_session_state=MarketSessionState.PRE_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=False, block_reason="pre-market -- the new-position scan window is not open yet",
)
_ALLOWED = ValidationCycleEligibility(
    as_of=datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc),
    market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=True, block_reason=None,
)
_WEEKEND = ValidationCycleEligibility(
    as_of=datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc),
    market_session_state=MarketSessionState.MARKET_CLOSED, is_trading_day=False,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=False, block_reason="today is not a trading day (weekend or U.S. market holiday)",
)

_POSITION_EXPIRATION = date(2026, 10, 23)


def _make_position(position_id: str = "p1", ticker: str = "SPY") -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
        expiration=_POSITION_EXPIRATION,
        legs=[
            PortfolioPositionLeg(right="P", side="sell", strike=450.0, entry_price=6.0),
            PortfolioPositionLeg(right="P", side="buy", strike=440.0, entry_price=3.0),
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0, opened_at=NOW - timedelta(days=5),
    )


def _seed_positions(environment, *positions: PortfolioPosition) -> None:
    ops = _ops_config_module.load_operations_config()
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
    portfolio = Portfolio(
        as_of=NOW, nav=100_000.0, cash=80_000.0 if positions else 100_000.0, peak_equity=100_000.0,
        positions=list(positions), sector_by_ticker={p.ticker: p.sector for p in positions},
    )
    portfolio_store.save(COHORT_ID, portfolio)


def _fresh_chain_for(ticker: str, *, as_of: datetime) -> OptionChain:
    underlying = UnderlyingQuote(symbol=ticker, bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=as_of, source="tradier")
    contracts = [
        OptionContract(
            underlying=ticker, option_symbol=f"{ticker}261023P00450000", expiration=_POSITION_EXPIRATION,
            strike=450.0, right=OptionRight.PUT, bid=4.5, ask=4.7, last=4.6, volume=100, open_interest=500,
            underlying_price=455.0, timestamp=as_of, source="tradier",
        ),
        OptionContract(
            underlying=ticker, option_symbol=f"{ticker}261023P00440000", expiration=_POSITION_EXPIRATION,
            strike=440.0, right=OptionRight.PUT, bid=1.8, ask=2.0, last=1.9, volume=100, open_interest=500,
            underlying_price=455.0, timestamp=as_of, source="tradier",
        ),
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=as_of, source="tradier")


class _PositionAwareFakeProvider:
    """Returns a fresh, Risk/quality-gate-passing chain for any ticker by
    default. `stale_tickers`/`raising_tickers` let a test force a
    specific symbol into the stale-data or provider-failure path without
    touching any other symbol -- the same per-symbol isolation doctrine
    `run_control_cycle`'s own `_quality_gate_market_data` already uses."""

    def __init__(self, *, stale_tickers: frozenset[str] = frozenset(), raising_tickers: frozenset[str] = frozenset()):
        self._stale_tickers = stale_tickers
        self._raising_tickers = raising_tickers
        self.requested_tickers: list[str] = []

    async def get_option_chain(self, symbol: str):
        self.requested_tickers.append(symbol)
        if symbol in self._raising_tickers:
            raise RuntimeError(f"simulated provider failure for {symbol}")
        as_of = datetime.now(timezone.utc)
        if symbol in self._stale_tickers:
            as_of = as_of - timedelta(hours=6)  # far beyond the 15-minute freshness tolerance
        return _fresh_chain_for(symbol, as_of=as_of)

    async def get_underlying_quote(self, symbol: str):
        return (await self.get_option_chain(symbol)).underlying

    async def close(self) -> None:
        return None


def _patch_env(monkeypatch):
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)


def _lifecycle_cycle_id() -> str:
    """PAPER_TRADING_V1.5.11: the lifecycle-only cycle id is now
    bucketed by the market-local (America/New_York) hour, not merely
    the whole day -- mirrors `_run_lifecycle_only_safety_check`'s own
    construction exactly."""
    local = datetime.now(timezone.utc).astimezone(EASTERN)
    return f"validation-{local.date().isoformat()}-lifecycle-{local.hour:02d}"


def _main_cycle_id() -> str:
    return f"validation-{datetime.now(timezone.utc).date().isoformat()}"


SCRIPT_PATH = __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts" / "run_validation_cycle.py"


@pytest.mark.asyncio
class TestScanWindowOpenZeroPositions:
    """Item 1: scan window OPEN + zero positions -> normal opportunity
    scanning behavior preserved. (The dedicated end-to-end scan assertion
    already lives in test_review_only_daily_cycle.py; this just confirms
    the new code path doesn't change the OPEN+zero-position branch.)"""

    async def test_open_gate_zero_positions_does_not_use_the_lifecycle_only_path(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        cycle = _load_script_module("_v159_open_zero", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert control_loop_store.get_cycle_record(_main_cycle_id()) is not None
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id()) is None


@pytest.mark.asyncio
class TestScanWindowClosedZeroPositions:
    """Item 2: scan window CLOSED + zero positions -> no new-position
    scan, no candidate, safe no-op -- byte-identical to pre-V1.5.9. The
    authoritative proof of this already exists and still passes
    unmodified in test_market_hours_gate.py
    (TestBackendPostOutsideSession/TestCliOutsideSession); this adds one
    direct confirmation at this file's own call site."""

    async def test_closed_gate_zero_positions_returns_false_and_persists_nothing(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        cycle = _load_script_module("_v159_closed_zero", SCRIPT_PATH)

        def _must_not_be_called():
            raise AssertionError("provider must never be constructed when the gate is closed and no positions exist")

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: _must_not_be_called())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is False

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert control_loop_store.get_cycle_record(_main_cycle_id()) is None
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id()) is None


@pytest.mark.asyncio
class TestScanWindowClosedOnePosition:
    """Item 3: scan window CLOSED + one existing position -> the
    lifecycle path is reached and actually evaluates it."""

    async def test_lifecycle_runs_for_a_single_existing_position(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_closed_one", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert provider.requested_tickers == ["SPY"]  # position ticker only, never a scan-universe ticker

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.positions_evaluated == 1
        assert control_loop_store.get_cycle_record(_main_cycle_id()) is None, (
            "the scan-eligible cycle id must remain untouched by a lifecycle-only run"
        )

        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == [], "no candidate may ever be created here"


@pytest.mark.asyncio
class TestScanWindowClosedMultiplePositions:
    """Item 4: scan window CLOSED + multiple existing positions ->
    lifecycle evaluates every one of them."""

    async def test_lifecycle_evaluates_every_existing_position(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"), _make_position("p2", "QQQ"))
        cycle = _load_script_module("_v159_closed_multi", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert sorted(provider.requested_tickers) == ["QQQ", "SPY"]

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.positions_evaluated == 2

        snapshots = control_loop_store.decision_snapshots_for_cycle(_lifecycle_cycle_id())
        evaluated_ids = {s.position_id for s in snapshots}
        assert evaluated_ids == {"p1", "p2"}


@pytest.mark.asyncio
class TestScanWindowClosedStaleData:
    """Item 5: scan window CLOSED + existing position + stale data ->
    fails closed / observable; never a false "safely evaluated" claim."""

    async def test_stale_market_data_fails_closed_for_the_position(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_closed_stale", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider(stale_tickers=frozenset({"SPY"}))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True  # the cycle itself still completes and is observable

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        # The quality gate rejects the stale chain before the position is
        # ever valued -- src.portfolio.control_loop's DATA_INSUFFICIENT
        # branch appends to its own in-memory snapshot list but, like
        # every other DATA_INSUFFICIENT/no-policy/unknown-policy early
        # exit in that loop, never calls control_loop_store
        # .append_decision_snapshot (only a fully-evaluated position's
        # snapshot is durably persisted) -- so the observable, durable
        # signal for "this position's data failed the quality gate" is
        # the cycle record's own symbols_failed/degraded_mode, exactly
        # like the provider-exception case above.
        assert lifecycle_record.degraded_mode is True
        assert "SPY" in lifecycle_record.symbols_failed
        assert lifecycle_record.positions_evaluated == 1, "the position was still COUNTED, never silently dropped"


@pytest.mark.asyncio
class TestScanWindowClosedProviderFailure:
    """Item 6: scan window CLOSED + existing position + provider failure
    -> observable degraded/failure behavior; no new candidate."""

    async def test_provider_exception_is_isolated_and_observable(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_closed_provider_fail", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider(raising_tickers=frozenset({"SPY"}))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True  # one bad symbol never aborts the cycle (Part 7 isolation doctrine)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.degraded_mode is True
        assert "SPY" in lifecycle_record.symbols_failed

        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []


@pytest.mark.asyncio
class TestScanWindowOpenWithExistingPosition:
    """Item 7: scan window OPEN + existing position -> lifecycle still
    runs (never suppressed by, or made subordinate to, opportunity
    scanning happening in the same cycle)."""

    async def test_lifecycle_runs_alongside_scanning_when_the_gate_is_open(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_open_with_position", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        main_record = control_loop_store.get_cycle_record(_main_cycle_id())
        assert main_record is not None
        assert main_record.positions_evaluated == 1, "lifecycle must evaluate the existing position in the same (OPEN) cycle"
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id()) is None, (
            "the lifecycle-only safety-net path must never run when the gate is already open"
        )


@pytest.mark.asyncio
class TestWeekendBehaviorExplicit:
    """Item 8: weekend/non-trading-day behavior, tested and documented
    explicitly rather than assumed identical to an ordinary pre/post-
    market gate closure. This codebase's own answer: a weekend routes
    through the SAME gate-closed branch (evaluate_validation_cycle_eligibility
    already returns validation_cycle_allowed=False with is_trading_day=False
    for a weekend) -- no separate weekend-specific code path exists. Existing
    positions are still fetched and evaluated; genuinely stale weekend data
    fails closed via the existing, unmodified freshness quality gate, exactly
    as item 5 already proves for an ordinary trading day. This test exists
    to make that a deliberate, asserted fact, not an accident."""

    async def test_weekend_lifecycle_check_runs_and_fails_closed_on_stale_data(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_weekend", SCRIPT_PATH)
        # A real weekend's "freshest available" quote is last Friday's
        # close -- always stale relative to a Saturday/Sunday `now`.
        provider = _PositionAwareFakeProvider(stale_tickers=frozenset({"SPY"}))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _WEEKEND)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None, "a weekend must still route through the lifecycle-only safety check"
        # Same reasoning as the stale-data test: a DATA_INSUFFICIENT
        # position's snapshot is never persisted to the store, only
        # returned in-memory -- the durable, observable signal is the
        # cycle record's own symbols_failed/degraded_mode.
        assert lifecycle_record.degraded_mode is True
        assert "SPY" in lifecycle_record.symbols_failed
        assert lifecycle_record.positions_evaluated == 1

        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []


@pytest.mark.asyncio
class TestIdempotency:
    """Item 9: repeated invocation must not duplicate lifecycle actions,
    candidates, orders, or fills."""

    async def test_second_same_day_invocation_with_gate_closed_is_a_documented_no_op(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_idempotent", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        first = await cycle.run_validation_cycle()
        second = await cycle.run_validation_cycle()
        assert first is True
        assert second is True
        # Only the FIRST call should have actually touched the provider --
        # the second is a pure idempotency no-op.
        assert provider.requested_tickers == ["SPY"]

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        snapshots = control_loop_store.decision_snapshots_for_cycle(_lifecycle_cycle_id())
        assert len(snapshots) == 1, "a second same-day invocation must never duplicate a lifecycle decision snapshot"

        idempotency = _idempotency_store(ops)
        assert idempotency.all() == [], "no order may ever be placed from this path, repeated calls included"


class TestDashboardCliEquivalence:
    """Item 10: the dashboard-triggered cycle and the CLI-triggered cycle
    must produce the same safety semantics -- proven by construction
    (src.dashboard.validation_ops.trigger_validation_cycle loads and
    calls the exact same scripts/run_validation_cycle.py::run_validation_cycle
    this file's other tests call directly), exercised here via the real
    FastAPI route."""

    def test_dashboard_post_reaches_the_lifecycle_only_path_for_an_existing_position(self, environment, monkeypatch):
        import src.dashboard.validation_ops as validation_ops
        from fastapi.testclient import TestClient

        from src.dashboard.app import app

        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        validation_ops._runner_module = None
        runner_module = validation_ops._load_runner_module()
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(runner_module, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        client = TestClient(app)
        resp = client.post("/api/validation-cycle/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "place_order(" not in body["log"]

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.positions_evaluated == 1

        validation_ops._runner_module = None


@pytest.mark.asyncio
class TestNoPositionOrOrderFromLifecycleOnlyPath:
    """Item 11: no existing position (nor any order/fill) can be created
    merely because the lifecycle path was reached outside the scan
    window -- the lifecycle-only branch is structurally incapable of
    this (OpportunityScanConfig is never built; no PaperBroker is even
    constructed), proven here end-to-end rather than just by code
    inspection."""

    async def test_lifecycle_only_path_creates_no_candidate_no_order_no_new_position(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _make_position("p1", "SPY"))
        cycle = _load_script_module("_v159_no_side_effects", SCRIPT_PATH)
        provider = _PositionAwareFakeProvider()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []

        idempotency = _idempotency_store(ops)
        assert idempotency.all() == [], "the lifecycle-only path never constructs a PaperBroker, so it cannot place an order"

        portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
        portfolio = portfolio_store.get(COHORT_ID)
        assert len(portfolio.positions) == 1, "the single seeded position must remain the only one -- none added, none removed"
        assert portfolio.positions[0].position_id == "p1"


def _review_store(ops):
    from src.review.candidates import SqliteCandidateReviewStore

    return SqliteCandidateReviewStore(ops.candidate_review_db_path)


def _idempotency_store(ops):
    from src.brokers.base import SqliteIdempotencyStore

    return SqliteIdempotencyStore(ops.account_state_db_path)
