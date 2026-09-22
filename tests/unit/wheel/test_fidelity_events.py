"""Step 22.2 Part 20/23: Wheel <-> Fidelity manual-execution wiring
tests. Assignment must be a reconciliation event, never a fabricated
order -- explicitly tested here."""
from __future__ import annotations

import pytest

from src.brokers.fidelity import ExecutionConfirmation, TicketStatus, confirm_fill, transition as ticket_transition
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import evaluate_trade_proposal
from src.wheel import fidelity_events, lifecycle
from tests.unit.risk.conftest import NOW, build_approved_csp_scenario

FIDELITY_CAPS = BrokerCapabilities(
    broker_name="fidelity", execution_mode="MANUAL", account_alias="OPTIONS_ACCOUNT",
    options_enabled=True, allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL"],
)


def _approved_csp_with_ticket():
    scenario = build_approved_csp_scenario()
    result = evaluate_trade_proposal(
        scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
        scenario.market_data, FIDELITY_CAPS, limits=scenario.limits, now=NOW,
    )
    return scenario, result


class TestBuildTicket:
    def test_ticket_requires_risk_approval(self):
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        from src.risk.engine import RiskDecisionResult
        from src.risk.reason_codes import RiskDecision

        rejected = RiskDecisionResult(decision=RiskDecision.REJECT, reason_codes=[], approved_contracts=None, message="no")
        with pytest.raises(fidelity_events.WheelTicketNotApprovedError):
            fidelity_events.build_wheel_csp_open_ticket(wheel, risk_result=rejected, now=NOW)

    def test_approved_proposal_produces_a_real_ticket(self):
        scenario, result = _approved_csp_with_ticket()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wticket = fidelity_events.build_wheel_csp_open_ticket(wheel, risk_result=result, now=NOW)
        assert wticket.wheel_id == "w1"
        assert wticket.ticket.status == TicketStatus.AWAITING_HUMAN


class TestRecordFill:
    def test_confirmed_fill_drives_lifecycle_with_real_fill_facts(self):
        scenario, result = _approved_csp_with_ticket()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wticket = fidelity_events.build_wheel_csp_open_ticket(wheel, risk_result=result, now=NOW)

        t = ticket_transition(wticket.ticket, TicketStatus.ORDER_ENTERED, at=NOW)
        conf = ExecutionConfirmation(confirmed_by="owner", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=0.31, confirmed_at=NOW)
        t = confirm_fill(t, conf)

        wheel2 = fidelity_events.record_wheel_csp_open_fill(wheel, ticket=t, commission=0.65, now=NOW)
        assert wheel2.state.value == "csp_open"
        assert wheel2.open_csp_cycle.premium_received_per_share == 0.31

    def test_unconfirmed_ticket_raises(self):
        scenario, result = _approved_csp_with_ticket()
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wticket = fidelity_events.build_wheel_csp_open_ticket(wheel, risk_result=result, now=NOW)
        with pytest.raises(ValueError):
            fidelity_events.record_wheel_csp_open_fill(wheel, ticket=wticket.ticket, commission=0.65, now=NOW)


class TestBuyToClose:
    def test_leg_must_be_csp_or_cc(self):
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        with pytest.raises(ValueError):
            fidelity_events.record_wheel_buy_to_close_fill(wheel, leg="bogus", fill_price_per_share=1.0, commission=0.65, now=NOW)


class TestAssignmentIsReconciliationNotAnOrder:
    def test_csp_assignment_never_touches_a_ticket(self):
        import inspect

        source = inspect.getsource(fidelity_events.record_wheel_csp_assignment)
        assert "FidelityTradeTicket(" not in source
        assert "generate_trade_ticket" not in source

    def test_cc_assignment_never_touches_a_ticket(self):
        import inspect

        source = inspect.getsource(fidelity_events.record_wheel_cc_assignment)
        assert "FidelityTradeTicket(" not in source
        assert "generate_trade_ticket" not in source

    def test_assignment_functions_only_forward_to_lifecycle(self):
        wheel = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="LOWP", now=NOW)
        wheel = lifecycle.open_csp(wheel, strike=9.5, expiration=NOW.date(), contracts=1, premium_per_share=0.3, commission=0.65, proposal_id=None, position_id=None, now=NOW)
        wheel2 = fidelity_events.record_wheel_csp_assignment(wheel, now=NOW)
        assert wheel2.state.value == "assigned_shares"
