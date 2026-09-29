"""PAPER_TRADING_V1.5.1, Step 2 acceptance test: the new-position daily
validation cycle's market-hours safety gate, exercised end-to-end
through the REAL production entry points -- the dashboard's
`POST /api/validation-cycle/run` route and
`scripts/run_validation_cycle.py`'s own `run_validation_cycle()` --
never just the library function in isolation. Runs entirely offline
against temporary sqlite files (the `environment` fixture from
`test_review_only_daily_cycle.py`), never `data/options_agent.db`,
never a live market-data provider, never the actual current clock.

Covers the numbered scenarios the task requires that are specific to
the BACKEND/CLI gate (the market-session decision itself is covered by
`tests/unit/portfolio/test_market_session.py`; the dashboard button
logic by `tests/frontend/operator_control.test.js`):
13. Backend POST outside session -> rejected before provider/data/
    persistence activity.
14. Backend POST inside session -> reaches the mocked runner path.
15. CLI outside session -> rejected before provider/data/persistence
    activity.
16. CLI inside session -> reaches the mocked normal path.
21. No provider call occurs when blocked.
22. No cycle record is persisted when blocked.
23. No snapshot is persisted when blocked.
24. Existing historical ControlCycleRecord with market_open=false
    remains readable.
25. Existing V1.4.x API/status consumers remain backward compatible.

Plus the mandatory EXISTING-POSITION SAFETY test: proof that this
step's gate does not, and structurally cannot, suppress existing-
position Lifecycle Engine/Risk kill-switch monitoring.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.dashboard.validation_ops as validation_ops
import src.portfolio.control_loop as control_loop_module
from src.dashboard.app import app
from src.dashboard.validation_ops import OperatorStatusView
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from src.portfolio.persistence import InMemoryControlLoopStore
from src.portfolio.cycle_record import ControlCycleRecord
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    REPO_ROOT,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

SCRIPT_PATH = REPO_ROOT / "scripts" / "run_validation_cycle.py"

_BLOCKED = ValidationCycleEligibility(
    as_of=datetime(2026, 9, 25, 13, 21, tzinfo=timezone.utc),
    market_session_state=MarketSessionState.PRE_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=False, block_reason="pre-market -- the 2026-09-25-style incident this gate prevents",
)
_ALLOWED = ValidationCycleEligibility(
    as_of=datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc),
    market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=True, block_reason=None,
)


class _ProviderThatMustNeverBeConstructed:
    """Standing in for `get_configured_market_data_provider` in every
    blocked-gate test below -- if the gate did not reject BEFORE the
    provider is touched, calling this raises immediately, failing the
    test loudly rather than silently passing."""

    def __call__(self):
        raise AssertionError(
            "get_configured_market_data_provider was called -- the market-hours gate did not "
            "reject before touching the market-data provider"
        )


@pytest.fixture(autouse=True)
def _reset_cached_runner_module():
    validation_ops._runner_module = None
    yield
    validation_ops._runner_module = None


def _assert_no_new_state(ops, *, expected_snapshot_count=1):
    """Common post-blocked-call assertions: no cycle record for today,
    no candidate, no snapshot beyond the fixture's own seeded Day-1
    snapshot, no order ever placed."""
    from src.brokers.base import SqliteIdempotencyStore
    from src.review.candidates import SqliteCandidateReviewStore
    from src.validation.protocol import load_validation_config
    from src.validation.session import SqliteValidationStore

    control_loop_store = validation_ops.SqliteControlLoopStore(ops.control_loop_db_path)
    today_cycle_id = f"validation-{datetime.now(timezone.utc).date().isoformat()}"
    assert control_loop_store.get_cycle_record(today_cycle_id) is None, (
        "no ControlCycleRecord may be persisted when the market-hours gate blocks"
    )

    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    assert review_store.all_candidates(cohort_id=COHORT_ID) == [], "no candidate may have been created"

    val_config = load_validation_config()
    validation_store = SqliteValidationStore(val_config.db_path)
    snapshots = validation_store.snapshots(cohort_id=COHORT_ID)
    assert len(snapshots) == expected_snapshot_count, (
        "no NEW DailySnapshot may have been recorded when the market-hours gate blocks"
    )

    idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
    assert idempotency.all() == [], "no order may ever be placed from the daily-cycle path"


# ------------------------------------------------------ backend (dashboard)


class TestBackendPostOutsideSession:
    def test_rejected_before_provider_data_or_persistence_activity(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        runner_module = validation_ops._load_runner_module()
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", _ProviderThatMustNeverBeConstructed())
        monkeypatch.setattr(runner_module, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        client = TestClient(app)
        resp = client.post("/api/validation-cycle/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "market-hours gate" in body["log"]
        assert "place_order(" not in body["log"]

        ops = _ops_config_module.load_operations_config()
        _assert_no_new_state(ops)


class TestBackendPostInsideSession:
    def test_reaches_the_mocked_runner_path(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        runner_module = validation_ops._load_runner_module()
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(runner_module, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        client = TestClient(app)
        resp = client.post("/api/validation-cycle/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

        ops = _ops_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        assert len(review_store.candidates_awaiting_human(cohort_id=COHORT_ID)) == 1


# ------------------------------------------------------------------- CLI


@pytest.mark.asyncio
class TestCliOutsideSession:
    async def test_rejected_before_provider_data_or_persistence_activity(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        cycle = _load_script_module("_gate_test_cli_blocked_module", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", _ProviderThatMustNeverBeConstructed())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle()
        assert ok is False

        ops = _ops_config_module.load_operations_config()
        _assert_no_new_state(ops)


@pytest.mark.asyncio
class TestCliInsideSession:
    async def test_reaches_the_mocked_normal_path(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        cycle = _load_script_module("_gate_test_cli_allowed_module", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        assert len(review_store.candidates_awaiting_human(cohort_id=COHORT_ID)) == 1


# --------------------------------------------------------- historical safety


class TestHistoricalRecordsRemainReadable:
    def test_existing_control_cycle_record_with_market_open_false_still_deserializes(self):
        """The exact shape of the real 2026-09-23 degraded/mock cycle
        record this gate exists to prevent a recurrence of -- proves
        Step 2 introduced no change to `ControlCycleRecord` itself that
        could break reading a historical row."""
        store = InMemoryControlLoopStore()
        historical = ControlCycleRecord(
            cycle_id="validation-2026-09-23", started_at=datetime(2026, 9, 23, 13, 21, tzinfo=timezone.utc),
            completed_at=datetime(2026, 9, 23, 13, 21, tzinfo=timezone.utc),
            market_open=False, provider="mock", provider_health_status="degraded",
            degraded_mode=True,
        )
        store.save_cycle_record(historical)
        reloaded = store.get_cycle_record("validation-2026-09-23")
        assert reloaded is not None
        assert reloaded.market_open is False
        assert reloaded.degraded_mode is True


class TestApiBackwardCompatibility:
    def test_operator_status_view_still_validates_from_an_old_shaped_payload(self):
        """An old (pre-Step-2) serialized OperatorStatusView JSON --
        missing every new market-session field entirely -- must still
        validate, with the new fields resolving to their safe (fail-
        closed) defaults, never a validation error."""
        old_shaped = {
            "configured": True, "cohort_id": COHORT_ID, "provider": {
                "provider": "tradier", "is_tradier_production": True, "ready": True, "detail": "ready",
            },
        }
        view = OperatorStatusView.model_validate(old_shaped)
        assert view.market_session_state is None
        assert view.is_trading_day is None
        assert view.validation_cycle_allowed is False
        assert view.validation_cycle_block_reason is None


# ----------------------------------------------------- existing-position safety


class TestExistingPositionSafetyNeverSuppressed:
    """Mandatory per the task spec: prove this step's gate does not,
    and structurally cannot, suppress existing-position Lifecycle
    Engine/Risk kill-switch monitoring."""

    def test_control_loop_module_never_imports_the_market_hours_gate(self):
        """The gate lives ONLY in scripts/run_validation_cycle.py and
        src/dashboard/validation_ops.py -- never inside
        src.portfolio.control_loop (run_control_cycle itself), which
        remains fully independent and callable by any future caller."""
        import inspect

        source = inspect.getsource(control_loop_module)
        assert "market_session" not in source
        assert "evaluate_validation_cycle_eligibility" not in source

    def test_run_control_cycle_still_evaluates_a_position_regardless_of_is_market_open(self):
        """run_control_cycle was ALREADY market-agnostic before Step 2
        (is_market_open is descriptive metadata only, never a skip
        condition) -- Step 2 changed nothing inside this function, so
        it must still evaluate an existing position identically whether
        is_market_open is True or False."""
        from datetime import date, timedelta

        from src.data.option_chain import OptionChain, OptionContract, OptionRight
        from src.data.quotes import UnderlyingQuote
        from src.lifecycle.persistence import InMemoryLifecycleStore
        from src.lifecycle.policies_library import policies_for_strategy
        from src.llm.schemas import StrategyType
        from src.portfolio.control_loop import ControlCycleInputs, run_control_cycle
        from src.portfolio.persistence import InMemoryControlLoopStore as _ICLS
        from src.risk.limits import get_default_limits
        from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
        from src.strategies.base import StrategyKind

        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)  # a Saturday -- market closed all day
        exp = date(2026, 10, 23)
        policy = policies_for_strategy(StrategyKind.PUT_CREDIT_SPREAD)[0]
        position = PortfolioPosition(
            position_id="p1", ticker="SPY", sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=exp,
            legs=[
                PortfolioPositionLeg(right="P", side="sell", strike=450.0, entry_price=6.0),
                PortfolioPositionLeg(right="P", side="buy", strike=440.0, entry_price=3.0),
            ],
            contracts=2, capital_at_risk=1400.0, max_loss=1400.0, opened_at=now - timedelta(days=5),
        )
        portfolio = Portfolio(as_of=now, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[position], sector_by_ticker={"SPY": "ETF"})
        underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=now, source="tradier")
        contracts = [
            OptionContract(underlying="SPY", option_symbol="SPY261023P00450000", expiration=exp, strike=450.0, right=OptionRight.PUT, bid=4.5, ask=4.7, last=4.6, volume=100, open_interest=500, underlying_price=455.0, timestamp=now, source="tradier"),
            OptionContract(underlying="SPY", option_symbol="SPY261023P00440000", expiration=exp, strike=440.0, right=OptionRight.PUT, bid=1.8, ask=2.0, last=1.9, volume=100, open_interest=500, underlying_price=455.0, timestamp=now, source="tradier"),
        ]
        chain = OptionChain(underlying=underlying, contracts=contracts, timestamp=now, source="tradier")
        extras = {
            "earnings_data_available": True, "days_to_earnings": 9999, "initial_credit": 3.0,
            "profit_capture_denominator": 600.0, "short_leg_is_itm": False, "short_leg_extrinsic_value": 4.6,
            "position_delta_abs": 0.20,
        }

        for is_market_open in (True, False):
            inputs = ControlCycleInputs(
                cycle_id="cycle-market-agnostic", as_of=now, portfolio=portfolio, limits=get_default_limits(),
                provider="tradier", provider_health_status="healthy", is_trading_day=False,
                is_market_open=is_market_open, fetch_results={"SPY": chain},
                lifecycle_store=InMemoryLifecycleStore(), control_loop_store=_ICLS(),
                policy_name_for_position={"p1": policy.name}, extra_monitoring_inputs={"p1": dict(extras)},
            )
            result = run_control_cycle(inputs)
            assert result.cycle_record.positions_evaluated == 1
            snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
            assert snap.recommended_action is not None  # a real lifecycle decision was produced either way
