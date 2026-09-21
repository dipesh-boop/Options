"""Step 22 Part 5: the persistence acceptance test. Builds a full,
realistic slice of a validation cohort's life -- cohort creation,
portfolio snapshots, a real pipeline-produced opportunity (with its
strategy alternatives, Devil's Advocate review, Portfolio Manager
decision, Risk Engine decision, and PaperBroker fill), and a closed
trade -- against a REAL on-disk `SqliteValidationStore`, then proves
every one of those records survives two full simulated application
restarts (destroy the Python object, construct a brand new one against
the same file, reconnect) with byte-for-byte identical values, never
reconstructed from anything but what is actually on disk.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline
from src.validation.counterfactual import StrategyAlternativeRecord
from src.validation.protocol import build_validation_manifest, build_validation_period, load_validation_config
from src.validation.records import CohortRecord, OpportunityRecord
from src.validation.session import DailySnapshot, RuleViolationRecord, SqliteValidationStore

from .conftest import all_strategy_fixtures, base_proposal, full_stages, make_chain, pipeline_request

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


async def _build_real_opportunity(cohort_id: str) -> OpportunityRecord:
    """Runs the REAL pipeline (same pattern as every other acceptance
    test) to get a genuine `QuantitativeAnalysis`/Devil's-Advocate/
    Portfolio-Manager/Risk-Engine/Order/Fidelity-ticket trail, plus a
    hand-built losing alternative -- never fabricated numbers."""
    fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
    proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
    market_data = make_chain(*fx.contracts)
    stages = full_stages(market_data=market_data)
    req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
    outcome = await run_order_pipeline(req, stages)
    assert outcome.status == PipelineStatus.FILLED

    from src.strategies.put_credit_spread import evaluate_put_credit_spread
    from src.data.option_chain import OptionRight as DataOptionRight
    from tests.unit.strategies.conftest import EXPIRATION as ALT_EXPIRATION, contract as alt_contract, limits as alt_limits

    alt_evaluation = evaluate_put_credit_spread(
        ticker="XYZ", expiration=ALT_EXPIRATION, short_put_contract=alt_contract(95, DataOptionRight.PUT, 2.8, 3.0),
        long_put_contract=alt_contract(90, DataOptionRight.PUT, 1.4, 1.6), spot=100.0, sigma=0.25, t=24 / 365,
        rate=0.04, days_to_expiry=24, num_contracts=1, limits=alt_limits(),
    )
    alternative = StrategyAlternativeRecord(
        opportunity_id="opp-restart-1", evaluation=alt_evaluation, was_selected=False,
        risk_decision="approve", selection_or_rejection_reason="lower expected value than the selected structure",
    )

    return OpportunityRecord(
        opportunity_id="opp-restart-1", cohort_id=cohort_id, ticker="SPY", created_at=NOW,
        cash_no_trade=False, alternatives=(alternative,), proposal_id=proposal.proposal_id,
        quantitative_analysis=outcome.quantitative_analysis,
        devils_advocate_review=outcome.devils_advocate_review.review if outcome.devils_advocate_review else None,
        portfolio_manager_decision=outcome.portfolio_manager_decision.decision if outcome.portfolio_manager_decision else None,
        risk_decision=outcome.risk_decision,
        pipeline_status=outcome.status.value,
    )


def _cohort_record(cohort_id: str) -> CohortRecord:
    config = load_validation_config()
    period = build_validation_period(date(2026, 9, 22), config)
    manifest = build_validation_manifest(
        manifest_id="manifest-restart-test", period=period, frozen_at=NOW, starting_nav=100_000.0,
        strategy_versions={"put_credit_spread": "v1"}, cohort_label="RESTART_TEST_COHORT",
    )
    return CohortRecord(cohort_id=cohort_id, cohort_name="Restart Recovery Test Cohort", status="active", created_at=NOW, manifest=manifest, started_at=NOW)


def _trade_record():
    from tests.unit.validation.conftest import _trade

    return _trade()


class TestFullPersistenceAndMultiRestartRecovery:
    @pytest.mark.asyncio
    async def test_survives_two_full_simulated_application_restarts_with_identical_data(self, tmp_path):
        db_path = tmp_path / "options_agent_restart_test.db"
        cohort_id = "cohort-restart-1"

        # ---- 1-10: build and populate everything against a fresh store ----
        store = SqliteValidationStore(db_path)
        cohort = _cohort_record(cohort_id)
        store.record_cohort(cohort)

        snapshot_1 = DailySnapshot(
            snapshot_date=date(2026, 9, 22), nav=100_000.0, cash=95_000.0, capital_deployed_pct=0.05,
            open_position_count=0, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=NOW,
        )
        store.record_snapshot(snapshot_1, cohort_id=cohort_id)

        opportunity = await _build_real_opportunity(cohort_id)
        store.record_opportunity(opportunity)

        snapshot_2 = DailySnapshot(
            snapshot_date=date(2026, 9, 23), nav=100_075.0, cash=94_500.0, capital_deployed_pct=0.055,
            open_position_count=1, drawdown_pct=0.0, per_strategy_nav={"put_credit_spread": 75.0}, recorded_at=NOW + timedelta(days=1),
        )
        store.record_snapshot(snapshot_2, cohort_id=cohort_id)

        trade = _trade_record()
        store.record_trade(trade, cohort_id=cohort_id)

        violation = RuleViolationRecord(
            violation_id="viol-restart-1", occurred_at=NOW, rule_name="test_rule", description="exercised for restart coverage",
        )
        store.record_violation(violation, cohort_id=cohort_id)

        cohort_before = store.get_cohort(cohort_id)
        snapshots_before = store.snapshots(cohort_id=cohort_id)
        trades_before = store.trades()
        opportunities_before = store.opportunities(cohort_id=cohort_id)
        violations_before = store.violations()

        # ---- 11-13: destroy the application/database object, build a new one, reconnect ----
        del store
        store_2 = SqliteValidationStore(db_path)

        # ---- 14-20: reconstruct everything, prove identical ----
        cohort_after = store_2.get_cohort(cohort_id)
        snapshots_after = store_2.snapshots(cohort_id=cohort_id)
        trades_after = store_2.trades()
        opportunities_after = store_2.opportunities(cohort_id=cohort_id)
        violations_after = store_2.violations()

        assert cohort_after == cohort_before == cohort
        assert cohort_after.manifest == cohort.manifest
        by_date = lambda snaps: sorted(snaps, key=lambda s: s.snapshot_date)
        assert by_date(snapshots_after) == by_date(snapshots_before) == by_date([snapshot_1, snapshot_2])
        assert {s.nav for s in snapshots_after} == {100_000.0, 100_075.0}
        assert trades_after == trades_before == [trade]
        assert opportunities_after == opportunities_before
        assert len(opportunities_after) == 1
        reconstructed_opp = opportunities_after[0]
        assert reconstructed_opp == opportunity
        # Explicitly re-check the pieces Part 5 names by name.
        assert reconstructed_opp.risk_decision == opportunity.risk_decision
        assert reconstructed_opp.quantitative_analysis == opportunity.quantitative_analysis
        assert reconstructed_opp.alternatives == opportunity.alternatives
        assert reconstructed_opp.alternatives[0].was_selected is False
        assert violations_after == violations_before == [violation]

        # ---- simulate ANOTHER restart -- data must still be intact ----
        del store_2
        store_3 = SqliteValidationStore(db_path)
        assert store_3.get_cohort(cohort_id) == cohort
        assert by_date(store_3.snapshots(cohort_id=cohort_id)) == by_date([snapshot_1, snapshot_2])
        assert store_3.trades() == [trade]
        assert store_3.opportunities(cohort_id=cohort_id) == [opportunity]
        assert store_3.violations() == [violation]

        # ---- idempotent retry: re-recording the exact same records
        # after a "restart" (simulating a caller that doesn't know
        # whether its previous write committed) must never duplicate.
        store_3.record_cohort(cohort)
        store_3.record_snapshot(snapshot_1, cohort_id=cohort_id)
        store_3.record_snapshot(snapshot_2, cohort_id=cohort_id)
        store_3.record_trade(trade, cohort_id=cohort_id)
        store_3.record_opportunity(opportunity)
        store_3.record_violation(violation, cohort_id=cohort_id)

        assert len(store_3.cohorts()) == 1
        assert len(store_3.snapshots(cohort_id=cohort_id)) == 2
        assert len(store_3.trades()) == 1
        assert len(store_3.opportunities(cohort_id=cohort_id)) == 1
        assert len(store_3.violations()) == 1


class TestReconciliationFailuresAreDurableAndNeverSilent:
    def test_a_recorded_reconciliation_failure_survives_a_restart_and_stays_unresolved_until_explicitly_resolved(self, tmp_path):
        from src.validation.session import ReconciliationFailureRecord

        db_path = tmp_path / "reconciliation_test.db"
        store = SqliteValidationStore(db_path)
        failure = ReconciliationFailureRecord(
            failure_id="fail-1", cohort_id="cohort-1", proposal_id="prop-1", detected_at=NOW,
            detail="order filled at broker but portfolio_update stage raised -- see pipeline rejected_stage",
        )
        store.record_reconciliation_failure(failure)
        assert store.reconciliation_failures(unresolved_only=True) == [failure]

        del store
        store_2 = SqliteValidationStore(db_path)
        reloaded = store_2.reconciliation_failures()
        assert reloaded == [failure]
        assert reloaded[0].resolved is False

        resolved = ReconciliationFailureRecord(
            failure_id="fail-1", cohort_id="cohort-1", proposal_id="prop-1", detected_at=NOW,
            detail=failure.detail, resolved=True, resolved_at=NOW + timedelta(hours=1), resolution_note="manually reconciled by operator",
        )
        store_2.record_reconciliation_failure(resolved)
        assert store_2.reconciliation_failures(unresolved_only=True) == []
        assert store_2.reconciliation_failures() == [resolved]

        del store_2
        store_3 = SqliteValidationStore(db_path)
        assert store_3.reconciliation_failures() == [resolved]
