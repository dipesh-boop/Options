"""Section 7: multi-leg stress test. Complements, rather than
duplicates, the existing Step 20A unit coverage in
`tests/unit/risk/test_multileg_strategies.py` (structural validation
of every named malformed shape at the schema layer) and
`tests/unit/brokers/test_paper_broker_multileg.py` (ratio-correct
fills/collateral). This file proves three things those don't:

1. Leg *list order* never matters -- `TradeProposal`'s own structural
   validator sorts by strike internally, so a shuffled leg list for a
   3-/4-leg strategy produces an identical approved order through the
   real, unmocked pipeline.
2. One bad leg (missing quote, stale quote, zero-liquidity quote) in a
   3-/4-leg combo invalidates the *entire* order -- never a partial
   structure with the bad leg silently dropped -- proven both through
   the full pipeline and directly against `PaperBroker.place_order`
   (whose own `_resolve_leg_quote` comprehension raises on the first
   bad leg before any fill logic runs at all).
3. `TradeProposal.expiration` is a single top-level field, not
   per-leg -- so "mismatched expiration across legs" has no
   representation in the schema to begin with; this is asserted
   directly against the schema rather than assumed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.llm.schemas import LegSide, OptionLeg, OptionRight, StrategyType, TradeProposal
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, MD_TS, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_contract, pipeline_request


class TestExpirationIsSingleFieldNotPerLeg:
    def test_option_leg_has_no_expiration_field_of_its_own(self):
        """A structural guarantee, not a behavioral one: there is no
        field on `OptionLeg` a caller could even set to a different
        expiration per leg -- "mismatched leg expirations" is not a
        reachable state, not merely a rejected one."""
        assert "expiration" not in OptionLeg.model_fields

    def test_tradeproposal_expiration_is_the_single_shared_field(self):
        assert "expiration" in TradeProposal.model_fields


class TestLegOrderNeverAffectsTheApprovedStructure:
    """`_validate_legs_match_strategy` sorts by strike internally for
    every multi-leg strategy -- proposing a shuffled leg list must
    produce an identical fill through the real pipeline."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", [StrategyType.LONG_CALL_BUTTERFLY, StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY])
    async def test_reversed_leg_list_order_fills_identically(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        market_data = make_chain(*fx.contracts)

        forward_proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        reversed_proposal = base_proposal(fx.strategy, list(reversed(fx.legs)), contracts_requested=1)

        forward_outcome = await run_order_pipeline(
            pipeline_request(proposal=forward_proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy]),
            full_stages(market_data=market_data),
        )
        reversed_outcome = await run_order_pipeline(
            pipeline_request(proposal=reversed_proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy]),
            full_stages(market_data=market_data),
        )

        assert forward_outcome.status == PipelineStatus.FILLED
        assert reversed_outcome.status == PipelineStatus.FILLED
        assert forward_outcome.risk_decision.max_loss == pytest.approx(reversed_outcome.risk_decision.max_loss)
        assert forward_outcome.risk_decision.capital_required == pytest.approx(reversed_outcome.risk_decision.capital_required)
        assert forward_outcome.fidelity_ticket.estimated_credit_debit == pytest.approx(reversed_outcome.fidelity_ticket.estimated_credit_debit)
        assert len(forward_outcome.fidelity_ticket.legs) == len(reversed_outcome.fidelity_ticket.legs)


class TestOneBadLegInvalidatesTheWholeMultiLegOrderThroughThePipeline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", [StrategyType.LONG_CALL_BUTTERFLY, StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY])
    async def test_one_missing_leg_contract_rejects_the_whole_combo(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        incomplete_market_data = make_chain(*fx.contracts[:-1])  # drop the last leg's quote entirely
        stages = full_stages(market_data=incomplete_market_data)
        req = pipeline_request(proposal=proposal, market_data=incomplete_market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.order is None
        assert outcome.fidelity_ticket is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", [StrategyType.LONG_CALL_BUTTERFLY, StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY])
    async def test_one_illiquid_zero_quote_leg_rejects_the_whole_combo(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        illiquid_contracts = list(fx.contracts)
        illiquid_contracts[-1] = illiquid_contracts[-1].model_copy(update={"bid": 0.0, "ask": 0.0})
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*illiquid_contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.order is None

    @pytest.mark.asyncio
    async def test_one_stale_leg_out_of_four_rejects_the_whole_iron_condor(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        stale_contracts = list(fx.contracts)
        stale_contracts[2] = stale_contracts[2].model_copy(update={"timestamp": NOW - timedelta(hours=6)})
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*stale_contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.SHORT_IRON_CONDOR])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED


class TestOneBadLegInvalidatesTheWholeMultiLegOrderInPaperBrokerDirectly:
    """Bypasses the Risk Engine's own (earlier) freshness gate entirely
    to prove `PaperBroker.attempt_fill` has its *own*, independent,
    fail-closed guarantee: `_resolve_leg_quote` is called for every leg
    via a plain list comprehension, so the first bad leg raises before
    `compute_fill`/`_apply_fill` ever runs for *any* leg -- a 4-leg
    order can never end up 3-legs-filled/1-leg-missing."""

    async def _broker(self) -> PaperBroker:
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        return broker

    def _iron_condor_request(self, *, stale_third_leg: bool = False) -> PlaceOrderRequest:
        return PlaceOrderRequest(
            client_order_id="ic-multileg-acceptance",
            legs=[
                OrderLeg(symbol="ic-lp", right=DataRight.PUT, strike=600.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
                OrderLeg(symbol="ic-sp", right=DataRight.PUT, strike=610.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="ic-sc", right=DataRight.CALL, strike=650.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="ic-lc", right=DataRight.CALL, strike=660.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
            ],
            limit_price=1.0,
        )

    def _chain(self, *, stale_third_leg: bool):
        third_leg_ts = (NOW - timedelta(hours=6)) if stale_third_leg else MD_TS
        return [
            make_contract(option_symbol="ic-lp", strike=600.0, right=DataRight.PUT, bid=0.55, ask=0.60),
            make_contract(option_symbol="ic-sp", strike=610.0, right=DataRight.PUT, bid=1.20, ask=1.30),
            make_contract(option_symbol="ic-sc", strike=650.0, right=DataRight.CALL, bid=1.15, ask=1.20, timestamp=third_leg_ts),
            make_contract(option_symbol="ic-lc", strike=660.0, right=DataRight.CALL, bid=0.47, ask=0.50),
        ]

    @pytest.mark.asyncio
    async def test_all_four_legs_fresh_fills_normally(self):
        broker = await self._broker()
        broker.update_market_data(make_chain(*self._chain(stale_third_leg=False)))
        order = await broker.place_order(self._iron_condor_request())
        assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_one_stale_leg_out_of_four_rejects_the_whole_order_not_three_legs(self):
        broker = await self._broker()
        broker.update_market_data(make_chain(*self._chain(stale_third_leg=True)))
        order = await broker.place_order(self._iron_condor_request())
        assert order.status == OrderStatus.REJECTED
        positions = await broker.get_positions()
        # No leg -- not even the three fresh ones -- was ever partially
        # applied: the position book is untouched by the rejected order.
        assert all(p.quantity == 0 for p in positions) or positions == []


class TestMalformedStrikeStructureRejectsAtConstructionForEveryMultiLegStrategy:
    """Reinforces (does not duplicate) the unit-level coverage: proves
    through THIS suite's own fixtures that a strike-order violation on
    the real, currently-used fixture legs is rejected, so any future
    fixture drift in `conftest.py` would be caught here too."""

    def test_butterfly_wings_swapped_with_middle_strike_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL_BUTTERFLY]
        broken_legs = [
            OptionLeg(right=OptionRight.CALL, strike=628.0, side=LegSide.BUY, quantity_ratio=1),  # was the SELL middle strike
            OptionLeg(right=OptionRight.CALL, strike=615.0, side=LegSide.SELL, quantity_ratio=2),  # was a BUY wing
            OptionLeg(right=OptionRight.CALL, strike=641.0, side=LegSide.BUY, quantity_ratio=1),
        ]
        with pytest.raises(ValidationError, match="long wings and a short middle strike"):
            base_proposal(fx.strategy, broken_legs, contracts_requested=1)

    def test_iron_condor_put_wing_overlapping_call_wing_rejects(self):
        """Every leg's own BUY/SELL side is individually correct (so the
        per-side checks all pass) but the put wing's short strike
        (655) sits above the call wing's short strike (650) -- the
        wings overlap instead of bracketing the short strikes from
        outside -- caught only by the final ordering check."""
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        broken_legs = [
            OptionLeg(right=OptionRight.PUT, strike=600.0, side=LegSide.BUY),
            OptionLeg(right=OptionRight.PUT, strike=655.0, side=LegSide.SELL),
            OptionLeg(right=OptionRight.CALL, strike=650.0, side=LegSide.SELL),
            OptionLeg(right=OptionRight.CALL, strike=660.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="strictly increasing strikes"):
            base_proposal(fx.strategy, broken_legs, contracts_requested=1)

    def test_iron_butterfly_mismatched_center_strikes_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_BUTTERFLY]
        broken_legs = [
            OptionLeg(right=OptionRight.PUT, strike=610.0, side=LegSide.BUY),
            OptionLeg(right=OptionRight.PUT, strike=627.0, side=LegSide.SELL),  # put center != call center below
            OptionLeg(right=OptionRight.CALL, strike=628.0, side=LegSide.SELL),
            OptionLeg(right=OptionRight.CALL, strike=646.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="same center strike"):
            base_proposal(fx.strategy, broken_legs, contracts_requested=1)

    def test_wrong_quantity_ratio_on_iron_condor_wing_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        broken_legs = [leg.model_copy(update={"quantity_ratio": 2}) if i == 0 else leg for i, leg in enumerate(fx.legs)]
        with pytest.raises(ValidationError, match="1:1:1:1 quantity ratio"):
            base_proposal(fx.strategy, broken_legs, contracts_requested=1)

    def test_duplicate_leg_at_the_same_strike_and_right_rejects_iron_condor(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        duplicated_legs = [fx.legs[0], fx.legs[0], fx.legs[2], fx.legs[3]]  # long put wing duplicated, short put missing
        with pytest.raises(ValidationError, match="long put wing and a short put closer to the money"):
            base_proposal(fx.strategy, duplicated_legs, contracts_requested=1)

    def test_missing_one_leg_of_four_rejects_at_construction(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_BUTTERFLY]
        with pytest.raises(ValidationError, match="exactly four legs"):
            base_proposal(fx.strategy, fx.legs[:-1], contracts_requested=1)
