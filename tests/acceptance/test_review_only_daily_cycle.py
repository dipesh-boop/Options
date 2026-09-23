"""Step 22.5 (PAPER_TRADING_V1.4.4) acceptance test: the full Review-Only
operational loop end-to-end, exercising the REAL operator scripts
(`scripts/run_validation_cycle.py`, `scripts/confirm_candidate.py`)
against a temporary, isolated set of sqlite files -- never
`data/options_agent.db`, never a live market-data provider. Runs in the
ordinary offline suite, never gated on a live Tradier token.

Proves, against the actual production entry points (not just the library
functions they're built from):
- the daily cycle never calls `PaperBroker.place_order` for a new
  position (exactly one `AWAITING_HUMAN` candidate, zero fills);
- the daily cycle is idempotent for the same day;
- `confirm_candidate.py` is the only path that can ever place an order at
  all, and does so exactly once even when invoked twice for the same
  candidate id.

The third test intentionally does NOT assert a `CONFIRMED` (filled)
outcome. `confirm_candidate.py` (unlike the unit tests in
`tests/unit/review/test_confirmation.py`) constructs its `PaperBroker`
with the real, unmodified production default -- `FillModel
.LIQUIDITY_ADJUSTED` -- and `src.risk.engine` always sets a fresh
candidate's limit price to the exact repriced mid. Under
`LIQUIDITY_ADJUSTED`, `compute_fill` always subtracts a strictly
positive slippage amount from that same mid before comparing it back
against that same mid-priced limit (see `src.brokers.paper
.compute_fill`/`price_satisfies_limit`) -- so a brand-new limit order
priced at mid can never clear on its first `attempt_fill`, by
construction, regardless of market data. This is exactly why every
other test in this codebase that needs an actual `FILLED` order
(including `tests/unit/review/test_confirmation.py`'s own
`_mid_fill_broker()` helper) explicitly overrides `fill_model=FillModel
.MID`. `confirm_candidate.py` never does that -- it is real operational
code, not a test -- so a fresh confirmation legitimately, deterministically
comes back `NO_FILL` here. That NO_FILL is itself exactly what this test
proves end-to-end: the order is placed exactly once (one row in
`SqliteIdempotencyStore`), the candidate is terminally resolved (`NO_FILL`
is one of `CandidateStatus`'s terminal states), and a second confirmation
attempt is refused as `ALREADY_RESOLVED` without ever placing a second
order -- i.e., confirmation is safely idempotent even in the realistic
case where the simulated market doesn't cooperate. The CONFIRMED/filled
code path itself is already covered, with a controlled fill model, by
`tests/unit/review/test_confirmation.py`.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import src.data.universe as universe_module
import src.portfolio.operations_config as operations_config_module
import src.validation.protocol as validation_protocol_module
from src.validation.cohort import start_new_cohort
from src.validation.records import CohortRecord
from src.validation.session import DailySnapshot, SqliteValidationStore
from tests.unit.review.conftest import EXPIRATION, make_chain

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
COHORT_ID = "acceptance-test-cohort"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeMarketDataProvider:
    """Always returns the same small-strike, tight-spread, Risk-approvable
    chain regardless of symbol -- never a network call.

    The runner scripts stamp `inputs.as_of`/`now` from the real wall clock
    (`datetime.now(timezone.utc)`) -- this is genuine operational code, not
    something that takes an injectable clock -- so every chain returned
    here must be stamped with the *actual* current time too, not the
    fixture's frozen `NOW`. Otherwise the Portfolio Control Loop's own
    quote-freshness quality gate (`src.data.provider.DEFAULT_MAX_QUOTE_AGE`,
    15 minutes) would reject it as stale the instant real time drifts more
    than 15 minutes past `NOW`, which is exactly what was happening before
    this fix (degraded_mode=True, zero candidates, both assertions failing).

    A small fixed buffer is subtracted rather than using the exact current
    instant: `TradeProposal` itself rejects a `data_timestamp` after its
    own `timestamp` (`src.llm.schemas`), and `generate_candidates` builds
    the proposal's `timestamp` from the *script's* own already-captured
    `now` -- a `now()` read here, moments later inside the fetch loop,
    would otherwise land a few microseconds *after* that already-captured
    `now` and trip that validator.
    """

    _QUOTE_BUFFER = timedelta(seconds=5)

    async def get_option_chain(self, symbol: str):
        return make_chain(as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER)

    async def get_underlying_quote(self, symbol: str):
        return make_chain(as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER).underlying

    async def close(self) -> None:
        return None


@pytest.fixture
def environment(tmp_path, monkeypatch):
    """Seeds an already-started cohort (mirroring an operator's real
    machine) into a temporary validation db, and points every new
    config loader at temporary files -- never the repository's own
    config/ or data/ directories."""
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
            }
        )
    )

    monkeypatch.setattr(validation_protocol_module, "DEFAULT_CONFIG_PATH", validation_yaml)
    monkeypatch.setattr(universe_module, "DEFAULT_CONFIG_PATH", universe_yaml)
    monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", operations_yaml)

    return tmp_path


@pytest.fixture
def scripts(environment, monkeypatch):
    # Step 22.6: run_validation_cycle.py now refuses to proceed past its
    # provider preflight unless OPTIONS_AGENT_DATA_PROVIDER resolves to
    # "tradier" with a token configured and the production base URL (the
    # default when OPTIONS_AGENT_TRADIER_BASE_URL is unset) -- satisfy
    # that preflight here so this fixture's own FakeMarketDataProvider
    # monkeypatch (below) is what actually serves the subsequent real
    # fetch, exactly as before. The preflight itself never touches the
    # network -- it only reads these two environment variables.
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

    cycle = _load_script_module("_acceptance_run_validation_cycle", REPO_ROOT / "scripts" / "run_validation_cycle.py")
    confirm = _load_script_module("_acceptance_confirm_candidate", REPO_ROOT / "scripts" / "confirm_candidate.py")
    monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
    monkeypatch.setattr(confirm, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
    return cycle, confirm


@pytest.mark.asyncio
class TestReviewOnlyDailyCycleEndToEnd:
    async def test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate(self, scripts):
        cycle, _confirm = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = operations_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(awaiting) == 1

        from src.brokers.base import SqliteIdempotencyStore

        idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
        assert idempotency.all() == []  # no order was ever placed

    async def test_running_the_daily_cycle_twice_the_same_day_is_a_no_op(self, scripts):
        cycle, _confirm = scripts
        await cycle.run_validation_cycle()

        ops = operations_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        first_candidates = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        second_candidates = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(second_candidates) == len(first_candidates)  # no duplicate candidate from a second scan

    async def test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice(self, scripts):
        """See the module docstring: under `confirm_candidate.py`'s real,
        unmodified production `PaperBroker` (default `FillModel
        .LIQUIDITY_ADJUSTED`), a brand-new mid-priced limit order cannot
        clear on its first `attempt_fill` -- `NO_FILL` is the correct,
        deterministic outcome here, not a test bug. What this proves is
        that `confirm_candidate.py` is the only path that ever reaches
        `PaperBroker.place_order`, that it does so exactly once, and that
        a second confirmation on the same (now-resolved) candidate is
        safely refused rather than placing a second order."""
        cycle, confirm = scripts
        await cycle.run_validation_cycle()

        ops = operations_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        candidate = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)[0]

        exit_code_1 = await confirm._run(candidate.candidate_id)
        assert exit_code_1 == 2  # NO_FILL -- a safety-driven non-fill outcome, not a script error

        resolved = review_store.get_candidate(candidate.candidate_id)
        assert resolved.status.value == "no_fill"
        assert resolved.is_terminal

        from src.brokers.base import SqliteIdempotencyStore

        idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
        assert len(idempotency.all()) == 1  # the order was placed (and is resting, unfilled) exactly once

        exit_code_2 = await confirm._run(candidate.candidate_id)
        assert exit_code_2 == 3  # ALREADY_RESOLVED -- refused before ever touching PaperBroker again
        assert len(idempotency.all()) == 1  # never a second order
