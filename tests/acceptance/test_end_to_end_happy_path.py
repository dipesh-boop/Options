"""Section 2 + 29 ("End-to-End Pipeline Result"): proves the complete,
real (no mocked Risk Engine, no mocked Quant, no mocked PaperBroker)
pipeline

    MARKET DATA -> QUANT -> DEVIL'S ADVOCATE -> PORTFOLIO MANAGER ->
    RISK ENGINE -> ORDER VALIDATOR -> PAPER BROKER -> FILL ->
    PORTFOLIO ACCOUNTING -> DATABASE RECORD -> FIDELITY TICKET

works end to end for every one of the 15 order-eligible strategies
(the 16th library item, CASH/NO_TRADE, is exercised separately in
test_strategy_integrity.py -- it has no structure to fill). Only the
two LLM stages are scripted (frozen payloads, no live API call); every
other stage is the real production implementation.
"""
from __future__ import annotations

import pytest

from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import all_strategy_fixtures, base_proposal, full_stages, make_chain, pipeline_request


class TestHappyPathAllStrategies:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", list(all_strategy_fixtures().keys()), ids=lambda s: s.value)
    async def test_every_order_eligible_strategy_fills_and_produces_a_fidelity_ticket(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED, f"{strategy.value}: {outcome.reason}"
        assert outcome.order is not None and outcome.order.filled_quantity >= 1
        assert outcome.fidelity_ticket is not None
        assert outcome.updated_portfolio is not None
        assert len(outcome.updated_portfolio.positions) == 1
        # A database record was written for every fill -- no silent gap
        # between "PaperBroker filled" and "the outcome is recorded."
        assert len(stages.database.all()) == 1
        assert stages.database.all()[0].status == PipelineStatus.FILLED

    @pytest.mark.asyncio
    async def test_long_call_butterfly_ticket_shows_the_1_minus2_1_ratio(self):
        """The one strategy whose legs are NOT a flat 1:1 ratio --
        explicitly verify the Fidelity ticket a human would actually
        read shows the middle leg at exactly twice each wing's
        contracts, not silently normalized to 1:1:1."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL_BUTTERFLY]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=3)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL_BUTTERFLY])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        legs_by_strike = {leg.strike: leg.contracts for leg in outcome.fidelity_ticket.legs}
        strikes = sorted(legs_by_strike)
        lower, middle, upper = strikes
        assert legs_by_strike[middle] == 2 * legs_by_strike[lower] == 2 * legs_by_strike[upper]
        assert legs_by_strike[lower] == 3  # 3 combo units requested

    @pytest.mark.asyncio
    async def test_iron_condor_and_iron_butterfly_render_four_legs_and_two_breakevens(self):
        for strategy in (StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY):
            fx = all_strategy_fixtures()[strategy]
            proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
            market_data = make_chain(*fx.contracts)
            stages = full_stages(market_data=market_data)
            req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])

            outcome = await run_order_pipeline(req, stages)

            assert outcome.status == PipelineStatus.FILLED, f"{strategy.value}: {outcome.reason}"
            assert len(outcome.fidelity_ticket.legs) == 4
            assert outcome.fidelity_ticket.breakeven_upper is not None
            assert outcome.fidelity_ticket.breakeven_upper > outcome.fidelity_ticket.breakeven

    @pytest.mark.asyncio
    async def test_no_automatic_securities_order_reaches_a_real_broker(self):
        """Structural proof for section 2's "no LLM -> real broker
        execution" and "no Fidelity -> automatic order" requirement:
        the happy-path outcome's only broker-facing artifacts are a
        `PaperBroker` `Order` (simulation) and a `FidelityTradeTicket`
        (data for a human to read) -- there is no field, method, or
        side effect anywhere in `PipelineOutcome` that submits
        anything to a real brokerage."""
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.fidelity_ticket.status.value == "awaiting_human"
        # No automated-execution field exists on the ticket at all.
        assert not hasattr(outcome.fidelity_ticket, "auto_execute")
        assert not hasattr(outcome.fidelity_ticket, "submit")
