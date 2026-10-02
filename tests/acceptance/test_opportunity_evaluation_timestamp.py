"""PAPER_TRADING_V1.5.7 acceptance test: reproduces the exact
2026-10-02 production defect, and proves its fix, at the REAL
`scripts/run_validation_cycle.py` entry point -- not just at the
isolated `generate_candidates`/orchestrator unit level.

Root cause (see `src.portfolio.orchestrator.OpportunityScanConfig
.evaluation_as_of`'s own field comment for the full architecture trace):
`run_validation_cycle.py` captures its cycle `now` BEFORE fetching
market data; a real market-data provider necessarily stamps
`OptionChain.timestamp` with a LATER `datetime.now(timezone.utc)`,
during that fetch. A `TradeProposal` built with `timestamp=<pre-fetch
now>` and `data_timestamp=<post-fetch chain timestamp>` therefore always
failed `TradeProposal`'s own (correct, never weakened here) "data_timestamp
cannot be after the proposal timestamp" integrity check -- silently,
because `src.portfolio.opportunity_scan.scan_and_rank_opportunities`'s
own per-ticker `except Exception: continue` swallowed it with zero
diagnostic (fixed separately -- see
`tests/unit/workflows/test_candidate_generation.py
::TestGenerationExceptionObservabilityV157`).

Telling detail confirming this was a REAL, already-known-about defect:
`tests/acceptance/test_review_only_daily_cycle.py`'s own
`FakeMarketDataProvider` subtracts a 5-second buffer from every chain
it returns specifically to avoid tripping this exact validator in
tests -- its own docstring says so. This file's `FakeLaggyMarketDataProvider`
deliberately does the OPPOSITE (stamps chains slightly AHEAD of the
instant it's called, simulating real network latency) to prove the
real fix -- not a test-only workaround -- makes that buffer unnecessary.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

import src.data.universe as universe_module
import src.portfolio.operations_config as operations_config_module
import src.validation.protocol as validation_protocol_module
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from src.validation.cohort import start_new_cohort
from src.validation.records import CohortRecord
from src.validation.session import DailySnapshot, SqliteValidationStore
from tests.unit.review.conftest import EXPIRATION, make_chain

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
COHORT_ID = "acceptance-test-cohort-eval-ts"

def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeLaggyMarketDataProvider:
    """Every chain is stamped with the REAL current instant at the
    moment the fetch call executes -- no artificial offset in either
    direction. This is exactly the real-production shape: the script's
    cycle `now` is captured BEFORE this call; the chain's own timestamp
    is captured DURING it, at least some nonzero real wall-clock time
    later (this is true even in a fast test process, down to
    microseconds) -- which is precisely the ordering that broke
    V1.5.6's `TradeProposal` integrity check. `evaluation_as_of`
    (captured by the fix, strictly after the whole fetch loop
    completes) is therefore guaranteed `>=` every such chain timestamp
    by construction, not by chance."""

    async def get_option_chain(self, symbol: str):
        return make_chain(as_of=datetime.now(timezone.utc))

    async def get_underlying_quote(self, symbol: str):
        return make_chain(as_of=datetime.now(timezone.utc)).underlying

    async def close(self) -> None:
        return None


@pytest.fixture
def environment(tmp_path, monkeypatch):
    validation_db = tmp_path / "validation.db"
    ops_db = tmp_path / "ops.db"

    store = SqliteValidationStore(validation_db)
    cohort = start_new_cohort(
        manifest_id="acceptance-manifest", start_date=date(2026, 9, 22), duration_days=90,
        frozen_at=NOW, starting_nav=100_000.0, strategy_versions={"cash_secured_put": "v1"},
        store=store, cohort_label=COHORT_ID,
    )
    store.record_cohort(
        CohortRecord(cohort_id=COHORT_ID, cohort_name="ACCEPTANCE_TEST", status="active", created_at=NOW, manifest=cohort.manifest, started_at=NOW)
    )
    store.record_snapshot(
        DailySnapshot(snapshot_date=date(2026, 9, 22), nav=100_000.0, cash=100_000.0, capital_deployed_pct=0.0, open_position_count=0, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=NOW),
        cohort_id=COHORT_ID,
    )

    validation_yaml = tmp_path / "validation.yaml"
    validation_yaml.write_text(
        yaml.safe_dump(
            {
                "validation_period": {"duration_days": 90, "checkpoint_days": [30, 60]},
                "sample_size": {"minimum_completed_trades": 50, "preferred_completed_trades": 100},
                "starting_capital": {"default_nav": 100_000.0},
                "research_targets": {
                    "annual_return_low_pct": 0.12, "annual_return_high_pct": 0.15,
                    "reference_min_sharpe": 1.0, "reference_max_acceptable_drawdown_pct": 0.15,
                },
                "statistics": {
                    "bootstrap_iterations": 100, "bootstrap_confidence_pct": 0.90,
                    "monte_carlo_iterations": 100, "var_confidence_pct": 0.95, "random_seed": 1,
                },
                "decision_quality": {"min_probability_of_profit": 0.50, "rejected_trade_min_sample_size": 20},
                "alerts": {"consecutive_loss_alert_count": 5, "weekly_loss_alert_pct": 0.05},
                "storage": {"db_path": str(validation_db)},
            }
        )
    )
    universe_yaml = tmp_path / "universe.yaml"
    universe_yaml.write_text(yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]}))
    operations_yaml = tmp_path / "operations.yaml"
    operations_yaml.write_text(
        yaml.safe_dump(
            {
                "cohort": {"cohort_id": COHORT_ID, "account_id": COHORT_ID},
                "market_regime": {"default_regime": "normal"},
                "storage": {
                    "account_state_db_path": str(ops_db), "control_loop_db_path": str(ops_db),
                    "lifecycle_db_path": str(ops_db), "candidate_review_db_path": str(ops_db),
                },
                "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
                "market_hours": {"scan_open_buffer_minutes": 5, "scan_close_buffer_minutes": 15},
                "risk_data_wiring": {
                    "enabled": False, "min_correlation_observations": 20, "correlation_lookback_days": 60,
                },
            }
        )
    )

    monkeypatch.setattr(validation_protocol_module, "DEFAULT_CONFIG_PATH", validation_yaml)
    monkeypatch.setattr(universe_module, "DEFAULT_CONFIG_PATH", universe_yaml)
    monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", operations_yaml)

    return tmp_path


@pytest.fixture
def scripts(environment, monkeypatch):
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

    cycle = _load_script_module("_acceptance_eval_ts_run_validation_cycle", REPO_ROOT / "scripts" / "run_validation_cycle.py")
    monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeLaggyMarketDataProvider())
    monkeypatch.setattr(
        cycle,
        "evaluate_validation_cycle_eligibility",
        lambda now, **kw: ValidationCycleEligibility(
            as_of=now, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
            regular_session_open=None, regular_session_close=None,
            validation_cycle_allowed=True, block_reason=None,
        ),
    )
    return cycle


@pytest.mark.asyncio
class TestOpportunityEvaluationTimestampFixV157:
    async def test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate(self, scripts):
        """Before this fix, this exact scenario -- a chain genuinely
        timestamped after the script's pre-fetch `now` -- produced ZERO
        candidates with NO diagnostic at all (the 2026-10-02 incident).
        After this fix, the scan evaluates against a POST-fetch
        timestamp and the candidate is built successfully."""
        cycle = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = operations_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(awaiting) == 1  # the candidate was NOT silently lost

        candidate = awaiting[0]
        # the required invariant, satisfied by construction, never by
        # loosening TradeProposal's own integrity check
        assert candidate.proposal.timestamp >= candidate.proposal.data_timestamp

        from src.brokers.base import SqliteIdempotencyStore

        idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
        assert idempotency.all() == []  # still never a PaperBroker order from this path

    async def test_control_cycle_record_shows_zero_silent_generation_exceptions(self, scripts):
        cycle = scripts
        await cycle.run_validation_cycle()

        ops = operations_config_module.load_operations_config()
        from src.portfolio.persistence import SqliteControlLoopStore

        store = SqliteControlLoopStore(ops.control_loop_db_path)
        record = store.get_cycle_record(f"validation-{NOW.date().isoformat()}")
        # cycle_id is date-derived from the real wall clock in this script
        # (not the fixture's frozen NOW) -- look up by what the script
        # itself would have used instead.
        if record is None:
            today_id = f"validation-{datetime.now(timezone.utc).date().isoformat()}"
            record = store.get_cycle_record(today_id)
        assert record is not None
        assert record.candidate_funnel is not None
        assert record.candidate_funnel.generation_exceptions == 0
        assert record.candidate_funnel.candidates_generated >= 1
