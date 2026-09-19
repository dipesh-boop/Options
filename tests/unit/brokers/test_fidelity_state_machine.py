"""Tests for the ticket status state machine: allowed transitions,
and — the central requirement — that FILLED/PARTIALLY_FILLED are
reachable only through confirm_fill() with a real ExecutionConfirmation,
never through transition()."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.fidelity import (
    ApprovedOrder,
    ExecutionConfirmation,
    FidelityLegAction,
    FidelityManualProvider,
    FidelityOrderLeg,
    InvalidTransitionError,
    TicketStatus,
    confirm_fill,
    transition,
)
from src.data.option_chain import OptionRight

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def _approved_order() -> ApprovedOrder:
    md = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
    return ApprovedOrder(
        risk_approval_id="risk-approval-123",
        account_alias="Individual Brokerage - Options",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=date(2026, 10, 16), contracts=2),
        ],
        quantity=2,
        limit_price=1.35,
        minimum_acceptable_price=1.25,
        time_in_force="DAY",
        estimated_credit_debit=1.35,
        net_bid=1.30,
        net_ask=1.40,
        max_profit=270.0,
        max_loss=730.0,
        breakeven=618.65,
        capital_at_risk=730.0,
        return_on_capital=270 / 730,
        profit_target=0.68,
        loss_management_rule="Close or roll if loss reaches 2x credit received.",
        DTE_management_rule="Review/close/roll according to strategy rules at 21 DTE.",
        management_dte=21,
        timestamp=NOW,
        market_data_timestamp=md,
    )


def _ticket_at(status: TicketStatus):
    """Builds a ticket already sitting at `status` by walking the real
    transition path from AWAITING_HUMAN, never by constructing the
    status directly — so these tests exercise the actual state machine,
    not a shortcut around it."""
    ticket = FidelityManualProvider().generate_trade_ticket(_approved_order())
    path = {
        TicketStatus.AWAITING_HUMAN: [],
        TicketStatus.ORDER_ENTERED: [TicketStatus.ORDER_ENTERED],
        TicketStatus.CANCELLED: [TicketStatus.ORDER_ENTERED, TicketStatus.CANCELLED],
    }[status]
    for step in path:
        ticket = transition(ticket, step, at=NOW)
    return ticket


class TestTransitionHappyPaths:
    def test_awaiting_human_to_order_entered(self):
        ticket = _ticket_at(TicketStatus.AWAITING_HUMAN)
        updated = transition(ticket, TicketStatus.ORDER_ENTERED, at=LATER)
        assert updated.status == TicketStatus.ORDER_ENTERED
        assert updated.status_updated_at == LATER

    def test_order_entered_to_cancelled(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)
        updated = transition(ticket, TicketStatus.CANCELLED, at=LATER)
        assert updated.status == TicketStatus.CANCELLED

    def test_awaiting_human_to_cancelled(self):
        ticket = _ticket_at(TicketStatus.AWAITING_HUMAN)
        updated = transition(ticket, TicketStatus.CANCELLED, at=LATER)
        assert updated.status == TicketStatus.CANCELLED

    def test_transition_returns_a_new_object_original_unchanged(self):
        ticket = _ticket_at(TicketStatus.AWAITING_HUMAN)
        updated = transition(ticket, TicketStatus.ORDER_ENTERED, at=LATER)
        assert ticket.status == TicketStatus.AWAITING_HUMAN  # original untouched
        assert updated is not ticket

    def test_any_non_terminal_status_can_expire(self):
        from src.brokers.fidelity import _ALLOWED_TRANSITIONS

        for status, allowed in _ALLOWED_TRANSITIONS.items():
            if status in (TicketStatus.FILLED, TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED):
                continue
            assert TicketStatus.EXPIRED in allowed, f"{status} cannot expire"


class TestFillStatusesUnreachableViaTransition:
    @pytest.mark.parametrize("target", [TicketStatus.FILLED, TicketStatus.PARTIALLY_FILLED])
    def test_transition_to_fill_status_always_raises(self, target: TicketStatus):
        for status in (TicketStatus.AWAITING_HUMAN, TicketStatus.ORDER_ENTERED):
            ticket = _ticket_at(status)
            with pytest.raises(InvalidTransitionError, match="confirm_fill"):
                transition(ticket, target, at=LATER)

    def test_transition_to_filled_raises_even_from_a_status_that_normally_allows_no_transitions(self):
        # Even a terminal-ish status must go through the same rejection
        # path, not a different one that might accidentally be lenient.
        ticket = _ticket_at(TicketStatus.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            transition(ticket, TicketStatus.FILLED, at=LATER)


class TestInvalidTransitions:
    def test_skipping_directly_from_proposed_to_risk_approved_rejected(self):
        # Ticket objects always start at AWAITING_HUMAN in this module
        # (generate_trade_ticket's only output), so simulate an earlier
        # pipeline stage's ticket-shaped object for this check via the
        # transition graph directly.
        from src.brokers.fidelity import _ALLOWED_TRANSITIONS

        assert TicketStatus.RISK_APPROVED not in _ALLOWED_TRANSITIONS[TicketStatus.PROPOSED]

    def test_cannot_transition_out_of_a_terminal_status(self):
        ticket = _ticket_at(TicketStatus.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            transition(ticket, TicketStatus.AWAITING_HUMAN, at=LATER)

    def test_cannot_re_enter_awaiting_human_from_order_entered(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)
        with pytest.raises(InvalidTransitionError):
            transition(ticket, TicketStatus.AWAITING_HUMAN, at=LATER)

    def test_cannot_go_backwards_from_order_entered_to_risk_approved(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)
        with pytest.raises(InvalidTransitionError):
            transition(ticket, TicketStatus.RISK_APPROVED, at=LATER)


class TestConfirmFillRequiresExplicitEvidence:
    def _confirmation(self, **overrides) -> ExecutionConfirmation:
        base = dict(confirmed_by="human:jane", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=LATER)
        base.update(overrides)
        return ExecutionConfirmation(**base)

    def test_confirm_fill_from_order_entered_succeeds(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)
        updated = confirm_fill(ticket, self._confirmation())
        assert updated.status == TicketStatus.FILLED
        assert updated.execution_confirmation is not None
        assert updated.execution_confirmation.confirmed_by == "human:jane"

    def test_confirm_fill_cannot_be_called_without_a_confirmation_object(self):
        # Structural proof: confirm_fill's signature requires it.
        import inspect

        sig = inspect.signature(confirm_fill)
        assert "confirmation" in sig.parameters
        assert sig.parameters["confirmation"].default is inspect.Parameter.empty

    def test_confirm_fill_from_awaiting_human_rejected(self):
        ticket = _ticket_at(TicketStatus.AWAITING_HUMAN)
        with pytest.raises(InvalidTransitionError, match="ORDER_ENTERED"):
            confirm_fill(ticket, self._confirmation())

    def test_confirm_fill_from_cancelled_rejected(self):
        ticket = _ticket_at(TicketStatus.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            confirm_fill(ticket, self._confirmation())

    def test_partial_fill_quantity_yields_partially_filled_status(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)  # quantity=2
        updated = confirm_fill(ticket, self._confirmation(filled_quantity=1))
        assert updated.status == TicketStatus.PARTIALLY_FILLED

    def test_full_fill_quantity_yields_filled_status(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)  # quantity=2
        updated = confirm_fill(ticket, self._confirmation(filled_quantity=2))
        assert updated.status == TicketStatus.FILLED

    def test_second_confirm_fill_on_partial_completes_the_fill(self):
        ticket = _ticket_at(TicketStatus.ORDER_ENTERED)
        partial = confirm_fill(ticket, self._confirmation(filled_quantity=1))
        assert partial.status == TicketStatus.PARTIALLY_FILLED
        completed = confirm_fill(partial, self._confirmation(filled_quantity=2))
        assert completed.status == TicketStatus.FILLED

    def test_risk_approved_cannot_reach_filled_without_passing_through_awaiting_human_and_order_entered(self):
        # There is no confirm_fill-reachable status other than
        # ORDER_ENTERED/PARTIALLY_FILLED - RISK_APPROVED is nowhere in
        # _FILL_CONFIRMABLE_FROM.
        from src.brokers.fidelity import _FILL_CONFIRMABLE_FROM

        assert TicketStatus.RISK_APPROVED not in _FILL_CONFIRMABLE_FROM
        assert TicketStatus.PROPOSED not in _FILL_CONFIRMABLE_FROM
        assert TicketStatus.AWAITING_HUMAN not in _FILL_CONFIRMABLE_FROM
