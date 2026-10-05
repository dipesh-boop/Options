"""PAPER_TRADING_V1.5.10 acceptance test: existing-position market-data
retrieval (both the gate-closed lifecycle-only safety check added in
V1.5.9, and the gate-open normal cycle's own existing-position fetch)
must cover each position's ACTUAL held expiration, never merely the
provider's nearest-N-by-calendar-date default or the global candidate-
entry [20, 45] DTE window. Exercises the REAL production entry point,
`scripts/run_validation_cycle.py`'s own `run_validation_cycle()` (and,
for item 13, the dashboard's `POST /api/validation-cycle/run` route),
against a fake provider that deliberately REPRODUCES
`TradierMarketDataProvider.get_option_chain`'s real "nearest
`max_expirations`, regardless of DTE" truncation -- unlike
`test_lifecycle_market_hours_separation.py`'s own
`_PositionAwareFakeProvider`, which always magically returns whatever a
position holds and so could never have caught this defect.

Covers the 15 edge cases PAPER_TRADING_V1.5.10's own task spec requires:
 1. held SPY position at 30 DTE while nearest-six expirations are all
    <20 DTE -> actual held expiration is explicitly retrieved and
    lifecycle evaluates it
 2. held QQQ position whose expiration is below candidate min_dte
    because the position has aged -> lifecycle still retrieves the
    ACTUAL held expiration
 3. held position near expiration -> exact held expiration retrieved
 4. two positions in the same ticker with different expirations -> both
    required expirations covered
 5. multi-leg spread with all legs at the same expiration -> all exact
    legs matched
 6. multi-leg structure with a required held contract missing -> fails
    closed (DATA_INSUFFICIENT); no partial valuation/action
 7. provider returns the wrong expiration -> rejected; no substitution
 8. provider returns the wrong strike/right -> rejected; no substitution
 9. stale exact held contract -> freshness gate still rejects/fails closed
10. provider failure for one ticker with another ticker healthy ->
    failure observable; healthy position still handled normally
11. gate closed -> no opportunity scan/candidate despite successful
    lifecycle data
12. gate open existing-position behavior -> the SAME shared retrieval
    helper also fixes the gate-open path for a position whose DTE falls
    outside BOTH the provider's nearest-N default AND the global scan
    window
13. dashboard and CLI -> same lifecycle safety semantics
14. idempotency -> V1.5.9's once-daily behavior is unchanged
15. no PaperBroker order/fill can be introduced by this retrieval fix
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import DteWindowOptionChainProvider
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio.account_state import SqlitePortfolioStore
from src.portfolio.persistence import SqliteControlLoopStore
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from tests.acceptance.test_lifecycle_market_hours_separation import (
    _ALLOWED,
    _BLOCKED,
    SCRIPT_PATH,
    _idempotency_store,
    _lifecycle_cycle_id,
    _main_cycle_id,
    _patch_env,
    _review_store,
)
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

_TODAY = datetime.now(timezone.utc).date()
_DEFAULT_LEGS = [(450.0, OptionRight.PUT), (440.0, OptionRight.PUT)]


def _seed_positions(environment, *positions: PortfolioPosition) -> None:
    ops = _ops_config_module.load_operations_config()
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
    portfolio = Portfolio(
        as_of=datetime.now(timezone.utc), nav=100_000.0, cash=80_000.0 if positions else 100_000.0,
        peak_equity=100_000.0, positions=list(positions),
        sector_by_ticker={p.ticker: p.sector for p in positions},
    )
    portfolio_store.save(COHORT_ID, portfolio)


def _position_at_dte(
    position_id: str, ticker: str, dte: int, *, legs: list[tuple[float, OptionRight]] = None,
) -> PortfolioPosition:
    legs = legs if legs is not None else _DEFAULT_LEGS
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
        expiration=_TODAY + timedelta(days=dte),
        legs=[
            PortfolioPositionLeg(right=right.value, side="sell" if i == 0 else "buy", strike=strike, entry_price=6.0 - i * 3.0)
            for i, (strike, right) in enumerate(legs)
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0,
        opened_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


def _contracts_for(ticker: str, expiration: date, specs: list[tuple[float, OptionRight]], *, as_of: datetime) -> list[OptionContract]:
    out = []
    for strike, right in specs:
        out.append(
            OptionContract(
                underlying=ticker, option_symbol=f"{ticker}{expiration.strftime('%y%m%d')}{right.value}{int(strike * 1000):08d}",
                expiration=expiration, strike=strike, right=right, bid=4.5, ask=4.7, last=4.6,
                volume=100, open_interest=500, underlying_price=455.0, timestamp=as_of, source="tradier",
            )
        )
    return out


class _DenseExpirationFakeProvider(DteWindowOptionChainProvider):
    """Reproduces `TradierMarketDataProvider.get_option_chain`'s real
    behavior -- nearest `max_expirations` calendar-date expirations,
    REGARDLESS of DTE -- and `get_option_chain_for_dte_window`'s real
    behavior -- only expirations whose DTE actually falls inside
    `[min_dte, max_dte]`. `slices_by_ticker[ticker]` is a list of
    `(expiration, contracts)` pairs; a position's own expiration is
    retrievable via the plain call ONLY if it happens to be among the
    nearest `max_expirations` dates for that ticker -- exactly the
    asymmetry PAPER_TRADING_V1.5.10 fixes for existing positions."""

    def __init__(
        self, *, slices_by_ticker: dict[str, list[tuple[date, list[OptionContract]]]], max_expirations: int = 6,
        stale_tickers: frozenset[str] = frozenset(), raising_tickers: frozenset[str] = frozenset(),
    ):
        self._slices_by_ticker = slices_by_ticker
        self._max_expirations = max_expirations
        self._stale_tickers = stale_tickers
        self._raising_tickers = raising_tickers
        self.get_option_chain_calls: list[str] = []
        self.get_option_chain_for_dte_window_calls: list[tuple[str, int, int]] = []

    def _as_of_for(self, symbol: str) -> datetime:
        as_of = datetime.now(timezone.utc)
        if symbol in self._stale_tickers:
            as_of -= timedelta(hours=6)  # far beyond the 15-minute freshness tolerance
        return as_of

    def _underlying(self, symbol: str, as_of: datetime) -> UnderlyingQuote:
        return UnderlyingQuote(symbol=symbol, bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=as_of, source="tradier")

    async def get_option_chain(self, symbol: str) -> OptionChain:
        self.get_option_chain_calls.append(symbol)
        if symbol in self._raising_tickers:
            raise RuntimeError(f"simulated provider failure for {symbol}")
        slices = sorted(self._slices_by_ticker.get(symbol, []), key=lambda s: s[0])[: self._max_expirations]
        as_of = self._as_of_for(symbol)
        contracts = [c for _, cs in slices for c in cs]
        return OptionChain(underlying=self._underlying(symbol, as_of), contracts=contracts, timestamp=as_of, source="tradier")

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date, diagnostics=None,
    ) -> OptionChain:
        self.get_option_chain_for_dte_window_calls.append((symbol, min_dte, max_dte))
        if symbol in self._raising_tickers:
            raise RuntimeError(f"simulated provider failure for {symbol}")
        eligible = [
            (exp, cs) for exp, cs in self._slices_by_ticker.get(symbol, []) if min_dte <= (exp - as_of).days <= max_dte
        ]
        chain_as_of = self._as_of_for(symbol)
        contracts = [c for _, cs in eligible for c in cs]
        return OptionChain(underlying=self._underlying(symbol, chain_as_of), contracts=contracts, timestamp=chain_as_of, source="tradier")

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self._underlying(symbol, self._as_of_for(symbol))

    async def close(self) -> None:
        return None


def _dense_near_term_slices(ticker: str, *, exclude_dte: set[int] = frozenset()) -> list[tuple[date, list[OptionContract]]]:
    """Six near-term expirations (1, 3, 5, 7, 9, 11 DTE) -- simulates a
    dense-expiration underlying like SPY/QQQ, all well under the
    candidate-entry [20, 45] DTE window and under any longer-dated held
    position's own DTE. These are what `get_option_chain`'s nearest-6
    default actually returns -- deliberately NOT including whatever DTE
    a test's held position is placed at."""
    out = []
    for dte in (1, 3, 5, 7, 9, 11):
        if dte in exclude_dte:
            continue
        expiration = _TODAY + timedelta(days=dte)
        out.append((expiration, _contracts_for(ticker, expiration, [(400.0, OptionRight.PUT)], as_of=datetime.now(timezone.utc))))
    return out


def _held_slice(ticker: str, dte: int, specs: list[tuple[float, OptionRight]] = None) -> tuple[date, list[OptionContract]]:
    specs = specs if specs is not None else _DEFAULT_LEGS
    expiration = _TODAY + timedelta(days=dte)
    return expiration, _contracts_for(ticker, expiration, specs, as_of=datetime.now(timezone.utc))


def _evaluated(control_loop_store, cycle_id: str, position_id: str) -> bool:
    """A position whose decision snapshot was persisted for this cycle
    was fully, successfully evaluated (OK) -- `run_control_cycle`'s
    DATA_INSUFFICIENT/no-policy/unknown-policy early-exit branches never
    call `append_decision_snapshot` (see V1.5.9's own finding), so
    absence here means fail-closed, never "evaluated but not recorded"."""
    snapshots = control_loop_store.decision_snapshots_for_cycle(cycle_id)
    return any(s.position_id == position_id for s in snapshots)


@pytest.mark.asyncio
class TestHeldExpirationOutsideNearestSix:
    """Item 1: held SPY position at 30 DTE while the nearest six
    expirations are all under 20 DTE -> the held expiration is
    explicitly retrieved and lifecycle evaluates it."""

    async def test_far_dated_position_is_retrieved_and_evaluated(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
        )
        cycle = _load_script_module("_v1510_outside_six", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert ("SPY", 30, 30) in provider.get_option_chain_for_dte_window_calls

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.positions_evaluated == 1
        assert "SPY" not in lifecycle_record.symbols_failed
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "the position's own 30 DTE expiration is outside the provider's nearest-6 default -- it "
            "must still be retrieved via the exact DTE-window request and fully evaluated, not DATA_INSUFFICIENT"
        )


@pytest.mark.asyncio
class TestHeldExpirationBelowCandidateMinDte:
    """Item 2: held QQQ position whose expiration is below the
    candidate-entry min_dte (20) because the position has aged ->
    lifecycle still retrieves the ACTUAL held expiration."""

    async def test_aged_below_min_dte_position_is_still_retrieved(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "QQQ", 10))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"QQQ": [*_dense_near_term_slices("QQQ", exclude_dte={11}), _held_slice("QQQ", 10)]},
        )
        cycle = _load_script_module("_v1510_below_min_dte", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "a position legitimately open below the candidate-entry DTE floor must still be monitored"
        )


@pytest.mark.asyncio
class TestHeldPositionNearExpiration:
    """Item 3: held position near its own expiration -> exact held
    expiration retrieved and evaluated."""

    async def test_near_expiration_position_is_retrieved(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 2))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY", exclude_dte={1, 3}), _held_slice("SPY", 2)]},
        )
        cycle = _load_script_module("_v1510_near_expiration", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1")


@pytest.mark.asyncio
class TestTwoExpirationsSameTicker:
    """Item 4: two positions in the same ticker with different
    expirations -> both required expirations are covered."""

    async def test_both_distinct_expirations_are_retrieved_and_evaluated(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30), _position_at_dte("p2", "SPY", 10))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [*_dense_near_term_slices("SPY", exclude_dte={11}), _held_slice("SPY", 30), _held_slice("SPY", 10)],
            },
        )
        cycle = _load_script_module("_v1510_two_expirations", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        requested_windows = {(min_dte, max_dte) for _, min_dte, max_dte in provider.get_option_chain_for_dte_window_calls}
        assert (30, 30) in requested_windows
        assert (10, 10) in requested_windows

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.positions_evaluated == 2
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1")
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p2")


@pytest.mark.asyncio
class TestMultiLegAllLegsSameExpiration:
    """Item 5: a multi-leg spread with all legs at the same expiration ->
    all exact legs matched (the two-leg put credit spread `_position_at_dte`
    already builds is exactly this shape)."""

    async def test_both_legs_of_the_spread_are_matched(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30, _DEFAULT_LEGS)]},
        )
        cycle = _load_script_module("_v1510_multileg_ok", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "both legs (450P short, 440P long) must be matched for the position to be fully evaluated"
        )


@pytest.mark.asyncio
class TestMultiLegMissingRequiredLeg:
    """Item 6: a multi-leg structure with one required held contract
    missing from the provider's response entirely -> fails closed
    (DATA_INSUFFICIENT); never a partial valuation or action."""

    async def test_missing_leg_fails_closed_never_partial(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        # Only the short leg (450 P) is ever returned, at any DTE -- the
        # long leg (440 P) genuinely does not exist anywhere in this
        # provider's data, simulating a provider gap rather than a
        # retrieval-window problem.
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30, [(450.0, OptionRight.PUT)])]},
        )
        cycle = _load_script_module("_v1510_missing_leg", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True  # the cycle itself still completes and is observable

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        # This is a PER-POSITION leg-matching failure inside
        # `revalue_position` (the chain itself is usable -- other
        # contracts in it are fine), not a per-TICKER chain-level
        # quality-gate failure -- `symbols_failed`/`degraded_mode` are
        # the wrong signal here (see items 9/10 for that case).
        # `recommendations_created` counts every decision attempt,
        # including a DATA_INSUFFICIENT one that control_loop.py never
        # persists to the store -- so a reached-but-not-persisted
        # decision is the correct, honest, durable proof of fail-closed.
        assert lifecycle_record.positions_evaluated == 1, "the position was counted, never silently dropped"
        assert lifecycle_record.recommendations_created == 1, "a DATA_INSUFFICIENT decision was reached for the position"
        assert not _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "one missing leg must fail the WHOLE position closed -- never a partial valuation using only the matched leg"
        )


@pytest.mark.asyncio
class TestWrongExpirationNeverSubstituted:
    """Item 7: a provider that returns contracts stamped with the WRONG
    expiration (never the position's own) -> rejected; no substitution.
    Proves `src.portfolio.revaluation`'s exact `(expiration, strike,
    right)` key lookup, not a nearest/approximate match."""

    async def test_wrong_expiration_contracts_are_never_matched(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        wrong_expiration = _TODAY + timedelta(days=31)  # one day off from the position's real 30 DTE expiration
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [
                    *_dense_near_term_slices("SPY"),
                    (wrong_expiration, _contracts_for("SPY", wrong_expiration, _DEFAULT_LEGS, as_of=datetime.now(timezone.utc))),
                ],
            },
        )
        cycle = _load_script_module("_v1510_wrong_expiration", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        # Per-leg matching failure, not a per-ticker chain/quality-gate
        # failure -- see item 6's own comment for why `recommendations_
        # created` (a decision was reached) plus absence from persisted
        # snapshots (never persisted for DATA_INSUFFICIENT) is the
        # correct durable signal here, not `symbols_failed`.
        assert lifecycle_record.recommendations_created == 1
        assert not _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "a contract at the wrong expiration must never be substituted for the position's real one"
        )


@pytest.mark.asyncio
class TestWrongStrikeOrRightNeverSubstituted:
    """Item 8: a provider that returns contracts at the right expiration
    but the WRONG strike/right -> rejected; no substitution."""

    async def test_wrong_strike_and_right_contracts_are_never_matched(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        # Right expiration, but neither strike nor right matches either
        # held leg (450P short / 440P long) -- a 500 CALL instead.
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30, [(500.0, OptionRight.CALL)])]},
        )
        cycle = _load_script_module("_v1510_wrong_strike_right", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.recommendations_created == 1
        assert not _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1")


@pytest.mark.asyncio
class TestStaleExactHeldContract:
    """Item 9: the exact held contract is retrieved but stale -> the
    freshness gate still rejects it / fails closed, even after the
    V1.5.10 retrieval fix."""

    async def test_stale_exact_contract_still_fails_closed(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
            stale_tickers=frozenset({"SPY"}),
        )
        cycle = _load_script_module("_v1510_stale_exact", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.degraded_mode is True
        assert "SPY" in lifecycle_record.symbols_failed
        assert not _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "retrieving the right contract is not enough -- a stale quote must still fail closed"
        )


@pytest.mark.asyncio
class TestOneTickerFailsAnotherHealthy:
    """Item 10: provider failure for one ticker with another ticker
    healthy -> the failure is observable; the healthy position is still
    handled according to the existing isolation doctrine."""

    async def test_failure_is_isolated_to_the_failing_ticker(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30), _position_at_dte("p2", "QQQ", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)],
                "QQQ": [*_dense_near_term_slices("QQQ"), _held_slice("QQQ", 30)],
            },
            raising_tickers=frozenset({"SPY"}),
        )
        cycle = _load_script_module("_v1510_one_fails", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        lifecycle_record = control_loop_store.get_cycle_record(_lifecycle_cycle_id())
        assert lifecycle_record is not None
        assert lifecycle_record.degraded_mode is True
        assert "SPY" in lifecycle_record.symbols_failed
        assert "QQQ" not in lifecycle_record.symbols_failed
        assert not _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1")
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p2"), (
            "one ticker's provider exception must never prevent another ticker's position from being evaluated"
        )


@pytest.mark.asyncio
class TestNoScanDespiteSuccessfulLifecycleData:
    """Item 11: gate closed -> no opportunity scan/candidate is ever
    created, even though the lifecycle-only retrieval fix now
    successfully retrieves and evaluates the position."""

    async def test_gate_closed_still_blocks_scanning_even_with_full_lifecycle_data(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
        )
        cycle = _load_script_module("_v1510_no_scan", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), "lifecycle data succeeded"
        assert control_loop_store.get_cycle_record(_main_cycle_id()) is None, "the scan-eligible cycle id must stay untouched"

        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == [], "no candidate may ever be created from this path"


@pytest.mark.asyncio
class TestGateOpenExistingPositionBenefitsFromSharedHelper:
    """Item 12: gate open existing-position behavior -> behaviorally
    equivalent to pre-V1.5.10 except for the intentionally shared
    retrieval helper. Uses a position at 10 DTE -- outside BOTH the
    provider's nearest-6 default AND the global [20, 45] scan window --
    so a correct evaluation here can only come from the new shared
    `_fetch_existing_position_chain` helper, never from the pre-existing
    universe-DTE-window coincidence the main cycle already had."""

    async def test_gate_open_position_outside_scan_window_is_still_retrieved(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 10))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [*_dense_near_term_slices("SPY", exclude_dte={11}), _held_slice("SPY", 10)],
                "QQQ": _dense_near_term_slices("QQQ"),
            },
        )
        cycle = _load_script_module("_v1510_gate_open_shared_helper", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert ("SPY", 10, 10) in provider.get_option_chain_for_dte_window_calls

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        main_record = control_loop_store.get_cycle_record(_main_cycle_id())
        assert main_record is not None
        assert _evaluated(control_loop_store, _main_cycle_id(), "p1"), (
            "the gate-open path's existing-position fetch must also use the exact-held-expiration helper, "
            "not merely rely on the ticker happening to be in the opportunity-scan universe"
        )
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id()) is None, "the gate is open -- the lifecycle-only path must never run"


class TestDashboardCliEquivalenceForExactRetrieval:
    """Item 13: the dashboard-triggered cycle and the CLI-triggered cycle
    produce the same retrieval/safety semantics for a position that
    specifically requires the V1.5.10 fix to be evaluated at all."""

    def test_dashboard_post_also_retrieves_the_far_dated_position(self, environment, monkeypatch):
        import src.dashboard.validation_ops as validation_ops
        from fastapi.testclient import TestClient

        from src.dashboard.app import app

        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        validation_ops._runner_module = None
        runner_module = validation_ops._load_runner_module()
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
        )
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
        assert _evaluated(control_loop_store, _lifecycle_cycle_id(), "p1"), (
            "the dashboard route delegates to the exact same run_validation_cycle() function, so it must "
            "benefit from the V1.5.10 fix identically to the CLI"
        )

        validation_ops._runner_module = None


@pytest.mark.asyncio
class TestIdempotencyUnchanged:
    """Item 14: V1.5.9's once-daily idempotency behavior is unchanged by
    this release -- a second same-day invocation still makes no
    additional provider calls at all."""

    async def test_second_same_day_invocation_still_a_pure_no_op(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
        )
        cycle = _load_script_module("_v1510_idempotent", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        first = await cycle.run_validation_cycle()
        second = await cycle.run_validation_cycle()
        assert first is True
        assert second is True
        assert provider.get_option_chain_calls == ["SPY"]
        assert provider.get_option_chain_for_dte_window_calls == [("SPY", 30, 30)]

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        snapshots = control_loop_store.decision_snapshots_for_cycle(_lifecycle_cycle_id())
        assert len(snapshots) == 1, "a second same-day invocation must never duplicate a lifecycle decision snapshot"


@pytest.mark.asyncio
class TestNoOrderFromExactRetrievalPath:
    """Item 15: no PaperBroker order/fill can be introduced by this
    retrieval fix -- the lifecycle-only path still never constructs a
    PaperBroker, regardless of how market data is fetched."""

    async def test_no_order_or_fill_from_the_fixed_retrieval_path(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices("SPY"), _held_slice("SPY", 30)]},
        )
        cycle = _load_script_module("_v1510_no_side_effects", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        idempotency = _idempotency_store(ops)
        assert idempotency.all() == [], "the lifecycle-only path never constructs a PaperBroker, so it cannot place an order"

        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []

        portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
        portfolio = portfolio_store.get(COHORT_ID)
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0].position_id == "p1"
