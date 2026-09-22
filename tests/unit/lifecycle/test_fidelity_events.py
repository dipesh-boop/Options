"""Part 21: Fidelity manual-execution wiring for lifecycle-driven
closes -- text instructions only, never a submitted order, and FILLED
requires an explicit human confirmation."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.data.option_chain import OptionRight
from src.lifecycle.fidelity_events import (
    LifecycleClosingAction,
    LifecycleClosingLegTicket,
    LifecycleTicketAlreadyFinalizedError,
    LifecycleTicketStatus,
    build_closing_ticket,
    record_ticket_cancelled,
    record_ticket_filled,
    render_closing_ticket_text,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _leg() -> LifecycleClosingLegTicket:
    return LifecycleClosingLegTicket(
        action=LifecycleClosingAction.BUY_TO_CLOSE, put_call=OptionRight.PUT, strike=95.0,
        expiration=date(2026, 2, 20), contracts=2,
    )


class TestBuildClosingTicket:
    def test_starts_awaiting_human(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="profit target reached", now=T0)
        assert ticket.status == LifecycleTicketStatus.AWAITING_HUMAN
        assert ticket.filled_at is None

    def test_roll_chain_id_recorded_when_present(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="roll", now=T0, roll_chain_id="ROLLCHAIN-T1")
        assert ticket.roll_chain_id == "ROLLCHAIN-T1"


class TestRenderClosingTicketText:
    def test_text_names_the_action_and_never_claims_submission(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="profit target reached", now=T0)
        text = render_closing_ticket_text(ticket)
        assert "BUY TO CLOSE" in text
        assert "XYZ" in text
        assert "No order has been submitted to any brokerage" in text


class TestFillLifecycle:
    def test_record_filled_transitions_to_filled(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="x", now=T0)
        filled = record_ticket_filled(ticket, filled_at=T0, fill_notes="filled at 0.10 net debit")
        assert filled.status == LifecycleTicketStatus.FILLED
        assert filled.fill_notes == "filled at 0.10 net debit"

    def test_cannot_fill_twice(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="x", now=T0)
        filled = record_ticket_filled(ticket, filled_at=T0)
        with pytest.raises(LifecycleTicketAlreadyFinalizedError):
            record_ticket_filled(filled, filled_at=T0)

    def test_cancel_transitions_to_cancelled(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="x", now=T0)
        cancelled = record_ticket_cancelled(ticket)
        assert cancelled.status == LifecycleTicketStatus.CANCELLED

    def test_cannot_cancel_an_already_filled_ticket(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="x", now=T0)
        filled = record_ticket_filled(ticket, filled_at=T0)
        with pytest.raises(LifecycleTicketAlreadyFinalizedError):
            record_ticket_cancelled(filled)

    def test_original_ticket_never_mutated(self):
        ticket = build_closing_ticket(trade_id="T1", ticker="XYZ", legs=[_leg()], reason="x", now=T0)
        record_ticket_filled(ticket, filled_at=T0)
        assert ticket.status == LifecycleTicketStatus.AWAITING_HUMAN
