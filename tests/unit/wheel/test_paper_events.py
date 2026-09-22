"""Step 22.2 Part 14/23: PaperBroker wheel event integration tests --
real fills (not simulated numbers), expiration settlement (assigned vs.
worthless), buy-to-close, and the Risk-Engine-approval requirement."""
from __future__ import annotations

import pytest

from src.brokers.base import Position
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.reason_codes import RiskDecision
from src.wheel import lifecycle, paper_events
from tests.unit.risk.conftest import NOW, build_approved_covered_call_scenario, build_approved_csp_scenario

INTERNAL_PAPER_CAPS = BrokerCapabilities(
    broker_name="internal_paper", execution_mode="AUTOMATED", account_alias="INTERNAL_PAPER",
    options_enabled=True, allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL"],
)


def _approve_csp():
    scenario = build_approved_csp_scenario()
    result = evaluate_trade_proposal(
        scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
        scenario.market_data, INTERNAL_PAPER_CAPS, limits=scenario.limits, now=NOW,
    )
    return scenario, result


def _approve_cc():
    scenario = build_approved_covered_call_scenario()
    result = evaluate_trade_proposal(
        scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
        scenario.market_data, INTERNAL_PAPER_CAPS, limits=scenario.limits, now=NOW,
    )
    return scenario, result


def _mid_broker(initial_cash: float = 90_000.0) -> PaperBroker:
    return PaperBroker(initial_cash=initial_cash, now=NOW, config=PaperBrokerConfig(fill_model=FillModel.MID))


class TestSubmitCspOpen:
    @pytest.mark.asyncio
    async def test_rejects_a_non_approved_risk_result(self):
        scenario, _ = _approve_csp()
        broker = _mid_broker()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        from src.risk.engine import RiskDecisionResult

        rejected = RiskDecisionResult(decision=RiskDecision.REJECT, reason_codes=[], approved_contracts=None, message="no")
        with pytest.raises(paper_events.WheelOrderNotApprovedError):
            await paper_events.submit_csp_open(broker, wheel, risk_result=rejected, proposal_id="p1", broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW)

    @pytest.mark.asyncio
    async def test_real_fill_transitions_wheel_with_actual_fill_facts(self):
        scenario, result = _approve_csp()
        assert result.decision == RiskDecision.APPROVE
        broker = _mid_broker()
        broker.update_market_data(scenario.market_data)
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)

        wheel, order = await paper_events.submit_csp_open(
            broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id,
            broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW,
        )
        assert order.status.value == "filled"
        assert wheel.state.value == "csp_open"
        cycle = wheel.open_csp_cycle
        assert cycle.premium_received_per_share == order.avg_fill_price
        assert cycle.contracts == order.filled_quantity
        assert cycle.proposal_id == scenario.proposal.proposal_id

    @pytest.mark.asyncio
    async def test_unfilled_order_leaves_wheel_unchanged(self):
        scenario, result = _approve_csp()
        # A broker with no market data loaded can never fill -- forces the "still resting" path.
        broker = PaperBroker(initial_cash=90_000.0, now=NOW)
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel2, order = await paper_events.submit_csp_open(
            broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id,
            broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW,
        )
        assert order.status.value == "rejected"  # no quote available -> PaperBroker rejects
        assert wheel2 == wheel  # never mutated / no cycle recorded


class TestSubmitCcOpen:
    @pytest.mark.asyncio
    async def test_real_fill_transitions_wheel(self):
        scenario, result = _approve_cc()
        assert result.decision == RiskDecision.APPROVE
        broker = _mid_broker()
        broker.update_market_data(scenario.market_data)
        broker._positions["LOWP"] = Position(
            symbol="LOWP", quantity=100, avg_cost=9.0, market_price=10.5, market_value=1050.0,
            unrealized_pnl=150.0, timestamp=NOW, source="paper",
        )
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel = wheel.model_copy(update={
            "state": __import__("src.wheel.state", fromlist=["WheelState"]).WheelState.CC_ELIGIBLE,
            "accounting": wheel.accounting.model_copy(update={"shares_owned": 100, "acquisition_basis_per_share": 9.0, "shares_acquired_at": NOW}),
        })

        wheel, order = await paper_events.submit_cc_open(
            broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id,
            broker_capabilities=INTERNAL_PAPER_CAPS, below_acquisition_basis=False, below_economic_basis=False,
            max_loss_if_called_away=None, now=NOW,
        )
        assert order.status.value == "filled"
        assert wheel.state.value == "cc_open"
        assert wheel.open_cc_cycle.contracts == order.filled_quantity


class TestSettlement:
    @pytest.mark.asyncio
    async def test_settle_csp_expiration_assigned(self):
        scenario, result = _approve_csp()
        broker = _mid_broker()
        broker.update_market_data(scenario.market_data)
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel, order = await paper_events.submit_csp_open(broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id, broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW)
        assert order.status.value == "filled"

        wheel = paper_events.settle_csp_expiration(broker, wheel, settlement_price=8.0, now=NOW)  # below the 9.5 strike -> ITM -> assigned
        assert wheel.state.value == "assigned_shares"
        assert wheel.accounting.shares_owned == 100

    @pytest.mark.asyncio
    async def test_settle_csp_expiration_worthless(self):
        scenario, result = _approve_csp()
        broker = _mid_broker()
        broker.update_market_data(scenario.market_data)
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel, order = await paper_events.submit_csp_open(broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id, broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW)

        wheel = paper_events.settle_csp_expiration(broker, wheel, settlement_price=20.0, now=NOW)  # above strike -> OTM
        assert wheel.state.value == "csp_expired"
        assert wheel.accounting.shares_owned == 0

    @pytest.mark.asyncio
    async def test_settle_with_no_open_cycle_raises(self):
        broker = _mid_broker()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        with pytest.raises(ValueError):
            paper_events.settle_csp_expiration(broker, wheel, settlement_price=10.0, now=NOW)


class TestBuyToClose:
    @pytest.mark.asyncio
    async def test_buy_to_close_csp_realizes_pnl(self):
        scenario, result = _approve_csp()
        broker = _mid_broker()
        broker.update_market_data(scenario.market_data)
        await broker.connect()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel, order = await paper_events.submit_csp_open(broker, wheel, risk_result=result, proposal_id=scenario.proposal.proposal_id, broker_capabilities=INTERNAL_PAPER_CAPS, now=NOW)
        assert order.status.value == "filled"

        wheel, close_order = await paper_events.buy_to_close_csp(broker, wheel, limit_price=10.0, now=NOW)
        assert close_order.status.value == "filled"
        assert wheel.state.value == "csp_closed"
        assert wheel.csp_cycles[0].realized_pnl is not None
