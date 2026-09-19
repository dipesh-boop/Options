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
