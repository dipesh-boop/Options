"""Section 10: PaperBroker lifecycle acceptance. `tests/unit/brokers/
test_paper_broker.py` already exhaustively covers every fill model, no
fill, partial fill, cancellation, duplicate-submission idempotency,
insufficient cash, and debit/credit collateral case for single- and
2-leg orders in isolation. This file complements that coverage at two
points it doesn't reach:

1. Every one of those same guarantees (duplicate-never-doubles,
   partial fill, cancellation) re-verified for 3-/4-leg orders
   specifically -- the platform's own `CLAUDE.md` names multi-leg
   quantity-ratio handling as its most fragile area, and the unit
   suite's duplicate/partial-fill/cancel tests all use 1-2 leg orders.
2. Full, unmocked pipeline-level accounting sanity: cash moves by
   exactly the expected net credit/debit minus commission, computed
   independently in this test, after a real `run_order_pipeline` call
   -- not just at the isolated `PaperBroker.place_order` level.
"""
from __future__ import annotations

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_contract, pipeline_request


class TestMultiLegDuplicateSubmissionNeverDoublesThePosition:
    """Extends `tests/unit/orchestration/test_pipeline.py`'s existing
    2-leg-only `TestDuplicateOrders` coverage to the 3-/4-leg
    structures, and additionally checks the position book and cash
    balance (not just fill count) are untouched by the retry."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", [StrategyType.LONG_CALL_BUTTERFLY, StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY])
    async def test_running_the_same_multileg_proposal_twice_is_idempotent(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        market_data = make_chain(*fx.contracts)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        broker.update_market_data(market_data)
        stages = full_stages(broker=broker, market_data=market_data)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])

        first = await run_order_pipeline(req, stages)
        cash_after_first = (await broker.get_account()).cash_balance
        positions_after_first = {p.symbol: p.quantity for p in await broker.get_positions()}

        second = await run_order_pipeline(req, stages)
        cash_after_second = (await broker.get_account()).cash_balance
        positions_after_second = {p.symbol: p.quantity for p in await broker.get_positions()}

        assert first.status == PipelineStatus.FILLED
        assert second.status == PipelineStatus.FILLED
        assert first.order.broker_order_id == second.order.broker_order_id
        assert cash_after_first == pytest.approx(cash_after_second)
        assert positions_after_first == positions_after_second


class TestMultiLegCancellation:
    def _resting_iron_condor_request(self) -> PlaceOrderRequest:
        # A limit far better than the real net credit can ever achieve
        # -- the order rests (SUBMITTED), never fills, exactly as
        # `tests/unit/brokers/test_paper_broker.py::TestNoFill` does for
        # a single leg; here with all 4 legs of a real combo.
        return PlaceOrderRequest(
            client_order_id="ic-cancel-acceptance",
            legs=[
                OrderLeg(symbol="cxl-lp", right=DataRight.PUT, strike=600.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
                OrderLeg(symbol="cxl-sp", right=DataRight.PUT, strike=610.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="cxl-sc", right=DataRight.CALL, strike=650.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="cxl-lc", right=DataRight.CALL, strike=660.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
            ],
            limit_price=100.0,  # demanding a $100/combo net credit -- unreachable
        )

    async def _broker(self) -> PaperBroker:
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        broker.update_market_data(make_chain(
            make_contract(option_symbol="cxl-lp", strike=600.0, right=DataRight.PUT, bid=0.55, ask=0.60),
            make_contract(option_symbol="cxl-sp", strike=610.0, right=DataRight.PUT, bid=1.20, ask=1.30),
            make_contract(option_symbol="cxl-sc", strike=650.0, right=DataRight.CALL, bid=1.15, ask=1.20),
            make_contract(option_symbol="cxl-lc", strike=660.0, right=DataRight.CALL, bid=0.47, ask=0.50),
        ))
        return broker

    @pytest.mark.asyncio
    async def test_unreachable_limit_rests_as_submitted_not_filled(self):
        broker = await self._broker()
        order = await broker.place_order(self._resting_iron_condor_request())
        assert order.status == OrderStatus.SUBMITTED
        assert order.filled_quantity == 0

    @pytest.mark.asyncio
    async def test_resting_multileg_order_can_be_cancelled_with_no_cash_or_position_impact(self):
        broker = await self._broker()
        cash_before = (await broker.get_account()).cash_balance
        order = await broker.place_order(self._resting_iron_condor_request())
        cancelled = await broker.cancel_order(order.client_order_id)
        assert cancelled.status == OrderStatus.CANCELLED
        cash_after = (await broker.get_account()).cash_balance
        assert cash_after == pytest.approx(cash_before)
        positions = await broker.get_positions()
        assert all(p.quantity == 0 for p in positions)

    @pytest.mark.asyncio
    async def test_filled_multileg_order_cannot_be_cancelled(self):
        broker = await self._broker()
        fillable_request = PlaceOrderRequest(
            client_order_id="ic-fillable-acceptance",
            legs=self._resting_iron_condor_request().legs,
            limit_price=1.0,  # matches the real fixture's net credit -- fills
        )
        order = await broker.place_order(fillable_request)
        assert order.status == OrderStatus.FILLED
        # A terminal (already-filled) order's cancel is a no-op --
        # never silently flips a real fill back to CANCELLED.
        unchanged = await broker.cancel_order(order.client_order_id)
        assert unchanged.status == OrderStatus.FILLED
        assert unchanged.filled_quantity == order.filled_quantity


class TestMultiLegPartialFillPreservesTheRatio:
    """Thin volume caps the number of COMBO units that can fill in one
    attempt -- proves the partial fill still respects the butterfly's
    1:-2:1 ratio exactly (never partially filling the middle leg out of
    proportion to the wings)."""

    @pytest.mark.asyncio
    async def test_thin_volume_partial_fill_keeps_the_1_minus2_1_ratio_intact(self):
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID, max_fill_fraction_of_volume=0.10, min_guaranteed_fill_contracts=2), now=NOW)
        await broker.connect()
        thin_chain = make_chain(
            make_contract(option_symbol="bf-lo", strike=615.0, right=DataRight.CALL, bid=16.4, ask=16.6, volume=20, open_interest=40),
            make_contract(option_symbol="bf-mid", strike=628.0, right=DataRight.CALL, bid=9.4, ask=9.6, volume=20, open_interest=40),
            make_contract(option_symbol="bf-hi", strike=641.0, right=DataRight.CALL, bid=4.4, ask=4.6, volume=20, open_interest=40),
        )
        broker.update_market_data(thin_chain)
        request = PlaceOrderRequest(
            client_order_id="bf-partial-acceptance",
            legs=[
                OrderLeg(symbol="bf-lo", right=DataRight.CALL, strike=615.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=50),
                OrderLeg(symbol="bf-mid", right=DataRight.CALL, strike=628.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=100),
                OrderLeg(symbol="bf-hi", right=DataRight.CALL, strike=641.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=50),
            ],
            limit_price=2.10,  # comfortably above the real ~$2.00 debit -- fills on price, thin volume caps quantity
        )
        order = await broker.place_order(request)

        assert order.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)
        assert 0 < order.filled_quantity < 50  # thin market caps well below the 50 requested combo units
        positions = {p.symbol: p for p in await broker.get_positions()}
        # Wings always exactly equal to each other, middle always exactly
        # 2x a wing -- whatever the actual filled combo-unit count is.
        assert positions["bf-lo"].quantity == order.filled_quantity
        assert positions["bf-hi"].quantity == order.filled_quantity
        assert positions["bf-mid"].quantity == -2 * order.filled_quantity


class TestCommissionAndAccountingThroughTheRealPipeline:
    @pytest.mark.asyncio
    async def test_credit_fill_cash_delta_matches_credit_minus_commission_independently_computed(self):
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        market_data = make_chain(*fx.contracts)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.65), now=NOW)
        await broker.connect()
        broker.update_market_data(market_data)
        stages = full_stages(broker=broker, market_data=market_data)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])

        cash_before = (await broker.get_account()).cash_balance
        outcome = await run_order_pipeline(req, stages)
        cash_after = (await broker.get_account()).cash_balance

        assert outcome.status == PipelineStatus.FILLED
        credit = 1.35 - 0.60  # short 620 put mid minus long 615 put mid, per conftest fixture
        expected_cash_delta = credit * 100 * 1 - 0.65 * 2  # 2 actual contracts filled (1 short + 1 long leg)
        assert (cash_after - cash_before) == pytest.approx(expected_cash_delta, abs=0.01)

    @pytest.mark.asyncio
    async def test_debit_fill_cash_delta_matches_debit_plus_commission_independently_computed(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        market_data = make_chain(*fx.contracts)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.65), now=NOW)
        await broker.connect()
        broker.update_market_data(market_data)
        stages = full_stages(broker=broker, market_data=market_data)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])

        cash_before = (await broker.get_account()).cash_balance
        outcome = await run_order_pipeline(req, stages)
        cash_after = (await broker.get_account()).cash_balance

        assert outcome.status == PipelineStatus.FILLED
        premium = (6.8 + 7.0) / 2
        expected_cash_delta = -premium * 100 * 1 - 0.65 * 1  # 1 actual contract, debit paid out
        assert (cash_after - cash_before) == pytest.approx(expected_cash_delta, abs=0.01)

    @pytest.mark.asyncio
    async def test_four_leg_commission_charges_per_actual_contract_across_all_four_legs(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        market_data = make_chain(*fx.contracts)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=1.0), now=NOW)
        await broker.connect()
        broker.update_market_data(market_data)
        stages = full_stages(broker=broker, market_data=market_data)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.SHORT_IRON_CONDOR])

        cash_before = (await broker.get_account()).cash_balance
        outcome = await run_order_pipeline(req, stages)
        cash_after = (await broker.get_account()).cash_balance

        assert outcome.status == PipelineStatus.FILLED
        credit = (1.25 - 0.575) + (1.175 - 0.485)
        expected_cash_delta = credit * 100 * 1 - 1.0 * 4  # 4 actual contracts, $1 commission each
        assert (cash_after - cash_before) == pytest.approx(expected_cash_delta, abs=0.01)
