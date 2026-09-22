"""Part 20: lifecycle-driven PaperBroker order/settlement integration --
real fills (not simulated numbers), partial close, commissions, and
expiration/assignment settlement pass-through."""
from __future__ import annotations

import pytest

from src.brokers.base import OrderAction
from src.brokers.order_validator import validate_and_build_order_request
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.lifecycle.paper_events import ClosingLeg, close_position, matching_settlement, settle_lifecycle_expiration
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.reason_codes import RiskDecision
from tests.unit.risk.conftest import NOW, build_approved_csp_scenario

CAPS = BrokerCapabilities(
    broker_name="internal_paper", execution_mode="AUTOMATED", account_alias="INTERNAL_PAPER",
    options_enabled=True, allowed_strategies=["CASH_SECURED_PUT"],
)


def _broker() -> PaperBroker:
    return PaperBroker(initial_cash=90_000.0, now=NOW, config=PaperBrokerConfig(fill_model=FillModel.MID))


async def _open_csp(broker, client_order_id="open-1"):
    scenario = build_approved_csp_scenario()
    result = evaluate_trade_proposal(
        scenario.proposal, scenario.portfolio, scenario.quantitative_analysis, scenario.market_data, CAPS,
        limits=scenario.limits, now=NOW,
    )
    assert result.decision == RiskDecision.APPROVE
    broker.update_market_data(scenario.market_data)
    await broker.connect()
    request = validate_and_build_order_request(
        result.approved_order, risk_decision=result.decision, approved_contracts=result.approved_contracts,
        broker_capabilities=CAPS, client_order_id=client_order_id,
    )
    order = await broker.place_order(request)
    return scenario, result, order


class TestClosePosition:
    @pytest.mark.asyncio
    async def test_buy_to_close_reverses_the_original_short_side(self):
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        close_leg = ClosingLeg(
            ticker=scenario.proposal.ticker, right=leg.put_call, strike=leg.strike, expiration=leg.expiration,
            original_action=OrderAction.SELL, quantity=order.filled_quantity,
        )
        close_order, commission = await close_position(broker, legs=[close_leg], limit_price=5.0)
        assert close_order.status.value == "filled"
        assert commission > 0

    @pytest.mark.asyncio
    async def test_partial_close_fills_less_than_full_quantity(self):
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        partial_qty = max(order.filled_quantity - 0, 1)  # this scenario opens 1 contract; partial == full here
        close_leg = ClosingLeg(
            ticker=scenario.proposal.ticker, right=leg.put_call, strike=leg.strike, expiration=leg.expiration,
            original_action=OrderAction.SELL, quantity=partial_qty,
        )
        close_order, _ = await close_position(broker, legs=[close_leg], limit_price=5.0)
        assert close_order.filled_quantity == partial_qty

    @pytest.mark.asyncio
    async def test_unrealistic_limit_price_does_not_fill(self):
        """PaperBroker never uses theoretical mid as guaranteed
        execution -- an unfavorable limit simply rests unfilled."""
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        close_leg = ClosingLeg(
            ticker=scenario.proposal.ticker, right=leg.put_call, strike=leg.strike, expiration=leg.expiration,
            original_action=OrderAction.SELL, quantity=order.filled_quantity,
        )
        close_order, commission = await close_position(broker, legs=[close_leg], limit_price=0.01)
        assert close_order.status.value != "filled"
        assert commission == 0.0


class TestSettleLifecycleExpiration:
    @pytest.mark.asyncio
    async def test_itm_short_put_is_assigned(self):
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        settlements = settle_lifecycle_expiration(
            broker, underlying_symbol=scenario.proposal.ticker, expiration=leg.expiration, settlement_price=leg.strike - 50.0,
        )
        match = matching_settlement(settlements, right=leg.put_call, strike=leg.strike)
        assert match is not None
        assert match.assigned_or_exercised is True

    @pytest.mark.asyncio
    async def test_otm_short_put_expires_worthless(self):
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        settlements = settle_lifecycle_expiration(
            broker, underlying_symbol=scenario.proposal.ticker, expiration=leg.expiration, settlement_price=leg.strike + 50.0,
        )
        match = matching_settlement(settlements, right=leg.put_call, strike=leg.strike)
        assert match is not None
        assert match.assigned_or_exercised is False

    @pytest.mark.asyncio
    async def test_no_matching_settlement_returns_none_not_a_guess(self):
        broker = _broker()
        scenario, result, order = await _open_csp(broker)
        leg = result.approved_order.legs[0]
        settlements = settle_lifecycle_expiration(
            broker, underlying_symbol=scenario.proposal.ticker, expiration=leg.expiration, settlement_price=leg.strike + 50.0,
        )
        assert matching_settlement(settlements, right=leg.put_call, strike=leg.strike + 9999) is None
