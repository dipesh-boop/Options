"""Tests for src.orchestration.pipeline (Step 12): the required 8-stage
order pipeline. "Every required stage must exist. Missing stage: REJECT
ORDER" plus the named test scenarios (duplicate orders, insufficient
cash, risk rejection, reprice required, expiration/assignment coverage
already proven at the PaperBroker level)."""
from __future__ import annotations

import dataclasses

import pytest

from src.brokers.order_validator import validate_and_build_order_request
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.llm.devils_advocate import evaluate_trade_risk as da_evaluate
from src.llm.portfolio_manager import evaluate_proposal as pm_evaluate
from src.orchestration.pipeline import (
    InMemoryDatabase,
    PipelineRequest,
    PipelineStages,
    PipelineStatus,
    SqliteDatabase,
    default_portfolio_update_stage,
    default_quant_stage,
    run_order_pipeline,
)
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal as risk_evaluate
from src.risk.limits import load_risk_limits
from tests.unit.orchestration.conftest import (
    MD_TS,
    NOW,
    client_returning,
    da_pass_payload,
    make_market_data,
    make_market_regime,
    make_portfolio,
    make_proposal,
    make_risk_reviewer_note,
    make_snapshot,
    pm_advance_payload,
)


def _full_stages(**overrides) -> PipelineStages:
    if "paper_broker" not in overrides:
        broker = PaperBroker(initial_cash=100_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        broker.update_market_data(make_market_data())
        overrides["paper_broker"] = broker

    base = dict(
        quant_stage=lambda proposal, market_data, portfolio, limits: default_quant_stage(proposal, market_data, portfolio, limits, now=NOW),
        devils_advocate_stage=lambda inputs: da_evaluate(inputs, client=client_returning(da_pass_payload()), system_prompt="DA"),
        portfolio_manager_stage=lambda inputs: pm_evaluate(inputs, client=client_returning(pm_advance_payload()), system_prompt="PM"),
        risk_engine_stage=risk_evaluate,
        order_validator_stage=validate_and_build_order_request,
        portfolio_update_stage=default_portfolio_update_stage,
        database=InMemoryDatabase(),
    )
    base.update(overrides)
    return PipelineStages(**base)


def _request(**overrides) -> PipelineRequest:
    limits = load_risk_limits()
    base = dict(
        proposal=make_proposal(),
        market_data=make_market_data(),
        portfolio=make_portfolio(),
        limits=limits,
        market_regime=make_market_regime(),
        risk_reviewer_note=make_risk_reviewer_note(),
        analysis_snapshot=make_snapshot(),
        current_snapshot=make_snapshot(),
        automated_broker_capabilities=load_broker_capabilities("internal_paper"),
        manual_broker_capabilities=load_broker_capabilities("fidelity"),
        now=NOW,
    )
    base.update(overrides)
    return PipelineRequest(**base)


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_pipeline_fills_and_produces_a_fidelity_ticket_at_the_same_time(self):
        stages = _full_stages()
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.FILLED
        assert outcome.order is not None and outcome.order.filled_quantity == 2
        assert outcome.fidelity_ticket is not None
        assert outcome.updated_portfolio is not None
        assert len(outcome.updated_portfolio.positions) == 1
        assert len(stages.database.all()) == 1
        assert stages.database.all()[0].status == PipelineStatus.FILLED


class TestCloseRollProposalsRegressionTS004:
    """TS-004 end to end: a CLOSE/ROLL proposal used to crash
    `run_order_pipeline` with an uncaught `AttributeError` (the Risk
    Engine approved it with no `approved_order`, and the Order
    Validator's first unguarded attribute access blew up). The full
    pipeline call must now return a clean, normal `PipelineOutcome`."""

    @pytest.mark.asyncio
    async def test_close_proposal_does_not_crash_the_pipeline(self):
        from src.llm.schemas import TradeAction

        stages = _full_stages()
        close_request = _request(proposal=make_proposal(action=TradeAction.CLOSE))
        outcome = await run_order_pipeline(close_request, stages)  # must not raise
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "risk_engine"
        assert "unsupported" in outcome.reason.lower() or "not yet a supported" in outcome.reason.lower()

    @pytest.mark.asyncio
    async def test_roll_proposal_does_not_crash_the_pipeline(self):
        from src.llm.schemas import TradeAction

        stages = _full_stages()
        roll_request = _request(proposal=make_proposal(action=TradeAction.ROLL))
        outcome = await run_order_pipeline(roll_request, stages)  # must not raise
        assert outcome.status == PipelineStatus.REJECTED


class TestSqliteDatabaseSurvivesRestartRegressionSY002:
    """SY-002: `InMemoryDatabase` loses every pipeline-run record on a
    process restart. `SqliteDatabase` is the durable alternative -- a
    record saved through one instance must still be readable by a
    brand-new instance pointed at the same file."""

    @pytest.mark.asyncio
    async def test_record_survives_a_simulated_process_restart(self, tmp_path):
        db_path = tmp_path / "pipeline.db"
        stages = _full_stages(database=SqliteDatabase(db_path))
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.FILLED
        del stages  # simulate the process crashing -- no Python object survives

        reopened = SqliteDatabase(db_path)
        records = reopened.all()
        assert len(records) == 1
        assert records[0].status == PipelineStatus.FILLED
        assert records[0].proposal_id == _request().proposal.proposal_id
        assert records[0].order is not None and records[0].order.filled_quantity == 2
        assert records[0].fidelity_ticket is not None


class TestPortfolioUpdateFailureRegressionSY003:
    """SY-003: an exception in the Portfolio-update stage used to be
    the one stage not wrapped in try/except -- it propagated straight
    out of `run_order_pipeline` uncaught, even though the fill had
    already happened (cash debited, Fill records appended), and no
    database record was ever written for it. These tests prove the
    real fill is never lost: the pipeline call always returns instead
    of raising, and a database record is always saved."""

    @pytest.mark.asyncio
    async def test_pipeline_call_does_not_raise_when_portfolio_update_stage_fails(self):
        def failing_portfolio_update(portfolio, proposal, qa, order):
            raise RuntimeError("boom: portfolio update exploded")

        stages = _full_stages(portfolio_update_stage=failing_portfolio_update)
        outcome = await run_order_pipeline(_request(), stages)  # must not raise
        assert outcome.rejected_stage == "portfolio_update"
        assert "boom: portfolio update exploded" in outcome.reason

    @pytest.mark.asyncio
    async def test_the_real_fill_is_still_reported_not_masked_as_a_rejection(self):
        """The order genuinely filled -- that fact must survive even
        though the portfolio update itself failed, so a human or
        downstream reconciliation never mistakes this for "nothing
        happened.\""""
        def failing_portfolio_update(portfolio, proposal, qa, order):
            raise RuntimeError("boom")

        stages = _full_stages(portfolio_update_stage=failing_portfolio_update)
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.FILLED
        assert outcome.order is not None and outcome.order.filled_quantity == 2
        assert outcome.updated_portfolio is None  # honestly absent, never fabricated

    @pytest.mark.asyncio
    async def test_a_database_record_is_still_written_despite_the_failure(self):
        """The permanent-gap half of SY-003: previously, nothing ever
        reached _record() on this path, so a retry with the same
        proposal would find PaperBroker's idempotency check satisfied
        and short-circuit without ever re-running the failing update
        logic -- silently losing this fill forever."""
        def failing_portfolio_update(portfolio, proposal, qa, order):
            raise RuntimeError("boom")

        stages = _full_stages(portfolio_update_stage=failing_portfolio_update)
        outcome = await run_order_pipeline(_request(), stages)
        records = stages.database.all()
        assert len(records) == 1
        assert records[0].status == PipelineStatus.FILLED
        assert records[0].order is not None and records[0].order.filled_quantity == 2


class TestMissingStages:
    @pytest.mark.parametrize(
        "stage_name",
        [
            "quant_stage",
            "devils_advocate_stage",
            "portfolio_manager_stage",
            "risk_engine_stage",
            "order_validator_stage",
            "paper_broker",
            "portfolio_update_stage",
            "database",
        ],
    )
    @pytest.mark.asyncio
    async def test_each_missing_stage_rejects_the_order(self, stage_name: str):
        stages = _full_stages(**{stage_name: None})
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == stage_name

    @pytest.mark.asyncio
    async def test_missing_stage_never_reaches_the_broker(self):
        broker = PaperBroker(initial_cash=100_000.0, now=NOW)
        broker.update_market_data(make_market_data())
        stages = _full_stages(paper_broker=broker, risk_engine_stage=None)
        await run_order_pipeline(_request(), stages)
        assert (await broker.get_open_orders()) == []
        assert (await broker.get_fills()) == []


class TestDevilsAdvocateRejection:
    @pytest.mark.asyncio
    async def test_devils_advocate_reject_verdict_stops_the_pipeline(self):
        stages = _full_stages(
            devils_advocate_stage=lambda inputs: da_evaluate(
                inputs, client=client_returning(da_pass_payload(verdict="REJECT", why_not_thesis="Too thin an edge for the tail risk.")), system_prompt="DA"
            )
        )
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "devils_advocate"
        assert outcome.portfolio_manager_decision is None  # never reached


class TestRepriceRequired:
    @pytest.mark.asyncio
    async def test_devils_advocate_stale_quotes_trigger_reprice_required(self):
        stale_current = make_snapshot(underlying_price=628.5 * 1.05)  # big move -> Python overrides to REPRICE_REQUIRED
        stages = _full_stages()
        outcome = await run_order_pipeline(_request(current_snapshot=stale_current), stages)
        assert outcome.status == PipelineStatus.REPRICE_REQUIRED
        assert outcome.devils_advocate_review.review.verdict == "REPRICE_REQUIRED"
        assert outcome.portfolio_manager_decision is None  # never reached


class TestPortfolioManagerOutcomes:
    @pytest.mark.asyncio
    async def test_reject_decision_stops_the_pipeline(self):
        stages = _full_stages(
            portfolio_manager_stage=lambda inputs: pm_evaluate(
                inputs, client=client_returning(pm_advance_payload(decision="reject", cash_preferred=False)), system_prompt="PM"
            )
        )
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "portfolio_manager"
        assert outcome.risk_decision is None  # never reached

    @pytest.mark.asyncio
    async def test_hold_cash_decision_yields_no_fill_not_a_rejection(self):
        stages = _full_stages(
            portfolio_manager_stage=lambda inputs: pm_evaluate(
                inputs, client=client_returning(pm_advance_payload(decision="hold_cash", cash_preferred=True)), system_prompt="PM"
            )
        )
        outcome = await run_order_pipeline(_request(), stages)
        assert outcome.status == PipelineStatus.NO_FILL
        assert outcome.rejected_stage is None
        assert outcome.risk_decision is None  # never reached


class TestRiskRejection:
    @pytest.mark.asyncio
    async def test_unknown_broker_capability_is_rejected_by_the_risk_engine(self):
        outcome = await run_order_pipeline(_request(automated_broker_capabilities=None), _full_stages())
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "risk_engine"

    @pytest.mark.asyncio
    async def test_excessive_drawdown_halts_before_any_order_is_placed(self):
        drawn_down_portfolio = make_portfolio(nav=80_000.0, cash=70_000.0, peak_equity=100_000.0)
        outcome = await run_order_pipeline(_request(portfolio=drawn_down_portfolio), _full_stages())
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "risk_engine"
        assert outcome.order is None


class TestInsufficientCash:
    @pytest.mark.asyncio
    async def test_insufficient_cash_is_caught_before_paper_broker_even_needed(self):
        poor_portfolio = make_portfolio(nav=100_000.0, cash=100.0, peak_equity=100_000.0)
        outcome = await run_order_pipeline(_request(portfolio=poor_portfolio), _full_stages())
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "risk_engine"
        assert outcome.order is None


class TestDuplicateOrders:
    @pytest.mark.asyncio
    async def test_running_the_same_proposal_twice_against_the_same_broker_is_idempotent(self):
        broker = PaperBroker(initial_cash=100_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        broker.update_market_data(make_market_data())
        stages = _full_stages(paper_broker=broker)

        first = await run_order_pipeline(_request(), stages)
        second = await run_order_pipeline(_request(), stages)

        assert first.status == PipelineStatus.FILLED
        assert second.status == PipelineStatus.FILLED
        assert first.order.broker_order_id == second.order.broker_order_id
        # Only one submission actually produced fills (2 legs).
        assert len(await broker.get_fills()) == 2


class TestOrderValidatorRejection:
    @pytest.mark.asyncio
    async def test_order_validator_rejects_when_target_broker_is_manual_not_automated(self):
        fidelity_caps = load_broker_capabilities("fidelity")
        outcome = await run_order_pipeline(_request(automated_broker_capabilities=fidelity_caps), _full_stages())
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage in ("risk_engine", "order_validator")
