"""Schema validation tests for ApprovedOrder, FidelityOrderLeg,
ExecutionConfirmation, and FidelityTradeTicket."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.brokers.fidelity import (
    MAX_MARKET_DATA_AGE,
    ApprovedOrder,
    ExecutionConfirmation,
    FidelityLegAction,
    FidelityOrderLeg,
    FidelityTradeTicket,
    TicketStatus,
)
from src.data.option_chain import OptionRight

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_FRESH = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)


def _legs():
    return [
        FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=2),
        FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=date(2026, 10, 16), contracts=2),
    ]


def _approved_kwargs(**overrides) -> dict:
    base = dict(
        risk_approval_id="risk-approval-123",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=_legs(),
        quantity=2,
        limit_price=1.35,
        estimated_credit_debit=1.35,
        max_profit=270.0,
        max_loss=730.0,
        breakeven=618.65,
        return_on_capital=270 / 730,
        profit_target=0.68,
        loss_management_rule="Close or roll if loss reaches 2x credit received.",
        DTE_management_rule="Review/close/roll according to strategy rules at 21 DTE.",
        timestamp=NOW,
        market_data_timestamp=MD_FRESH,
    )
    base.update(overrides)
    return base


def _ticket_kwargs(**overrides) -> dict:
    base = dict(
        trade_id="TICKET-1",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=_legs(),
        quantity=2,
        limit_price=1.35,
        estimated_credit_debit=1.35,
        max_profit=270.0,
        max_loss=730.0,
        breakeven=618.65,
        return_on_capital=270 / 730,
        profit_target=0.68,
        loss_management_rule="Close or roll if loss reaches 2x credit received.",
        DTE_management_rule="Review/close/roll according to strategy rules at 21 DTE.",
        market_data_timestamp=MD_FRESH,
        risk_approval_id="risk-approval-123",
        status=TicketStatus.AWAITING_HUMAN,
        status_updated_at=NOW,
        timestamp=NOW,
        source="fidelity_manual",
    )
    base.update(overrides)
    return base


class TestFidelityOrderLeg:
    def test_valid_leg(self):
        leg = FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=2)
        assert leg.contracts == 2

    def test_all_four_actions_valid(self):
        for action in FidelityLegAction:
            FidelityOrderLeg(action=action, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=1)

    def test_non_positive_contracts_rejected(self):
        with pytest.raises(ValidationError):
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=0)

    def test_non_positive_strike_rejected(self):
        with pytest.raises(ValidationError):
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=0, expiration=date(2026, 10, 16), contracts=1)

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            FidelityOrderLeg.model_validate({"action": "yolo_open", "put_call": "P", "strike": 620.0, "expiration": "2026-10-16", "contracts": 1})

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            FidelityOrderLeg.model_validate({"action": "sell_to_open", "put_call": "P", "strike": 620.0, "expiration": "2026-10-16", "contracts": 1, "execute": True})


class TestApprovedOrder:
    def test_valid_order(self):
        order = ApprovedOrder(**_approved_kwargs())
        assert order.ticker == "SPY"

    def test_stale_market_data_rejected(self):
        stale = NOW - MAX_MARKET_DATA_AGE - timedelta(seconds=1)
        with pytest.raises(ValidationError, match="stale"):
            ApprovedOrder(**_approved_kwargs(market_data_timestamp=stale))

    def test_market_data_exactly_at_boundary_accepted(self):
        boundary = NOW - MAX_MARKET_DATA_AGE
        order = ApprovedOrder(**_approved_kwargs(market_data_timestamp=boundary))
        assert order.market_data_timestamp == boundary

    def test_market_data_from_the_future_rejected(self):
        with pytest.raises(ValidationError):
            ApprovedOrder(**_approved_kwargs(market_data_timestamp=NOW + timedelta(minutes=1)))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            ApprovedOrder(**_approved_kwargs(timestamp=datetime(2026, 9, 20, 14, 0)))

    def test_naive_market_data_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            ApprovedOrder(**_approved_kwargs(market_data_timestamp=datetime(2026, 9, 20, 13, 55)))

    def test_no_broker_order_id_or_execution_field(self):
        forbidden = ["broker_order_id", "execute", "authorized", "confirmed", "filled"]
        for name in forbidden:
            assert name not in ApprovedOrder.model_fields

    def test_empty_legs_rejected(self):
        with pytest.raises(ValidationError):
            ApprovedOrder(**_approved_kwargs(legs=[]))

    def test_extra_field_rejected(self):
        payload = {**_approved_kwargs(), "execute_immediately": True}
        with pytest.raises(ValidationError):
            ApprovedOrder.model_validate(payload)


class TestExecutionConfirmation:
    def test_valid_confirmation(self):
        conf = ExecutionConfirmation(confirmed_by="human:jane", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=NOW)
        assert conf.confirmation_source == "human_manual_entry"

    def test_invalid_confirmation_source_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionConfirmation.model_validate({"confirmed_by": "x", "confirmation_source": "automated_bot", "filled_quantity": 1, "fill_price": 1.0, "confirmed_at": NOW})

    def test_non_positive_filled_quantity_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionConfirmation(confirmed_by="x", confirmation_source="human_manual_entry", filled_quantity=0, fill_price=1.0, confirmed_at=NOW)

    def test_naive_confirmed_at_rejected(self):
        with pytest.raises(ValidationError):
            ExecutionConfirmation(confirmed_by="x", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=1.0, confirmed_at=datetime(2026, 9, 20, 14, 0))


class TestFidelityTradeTicket:
    def test_valid_ticket(self):
        ticket = FidelityTradeTicket(**_ticket_kwargs())
        assert ticket.status == TicketStatus.AWAITING_HUMAN
        assert ticket.execution_confirmation is None

    def test_is_frozen(self):
        ticket = FidelityTradeTicket(**_ticket_kwargs())
        with pytest.raises(ValidationError):
            ticket.status = TicketStatus.FILLED  # type: ignore[misc]

    def test_stale_market_data_rejected(self):
        stale = NOW - MAX_MARKET_DATA_AGE - timedelta(seconds=1)
        with pytest.raises(ValidationError, match="stale"):
            FidelityTradeTicket(**_ticket_kwargs(market_data_timestamp=stale))

    def test_filled_status_without_confirmation_rejected(self):
        with pytest.raises(ValidationError, match="execution_confirmation"):
            FidelityTradeTicket(**_ticket_kwargs(status=TicketStatus.FILLED))

    def test_partially_filled_without_confirmation_rejected(self):
        with pytest.raises(ValidationError, match="execution_confirmation"):
            FidelityTradeTicket(**_ticket_kwargs(status=TicketStatus.PARTIALLY_FILLED))

    def test_confirmation_present_but_status_not_fill_rejected(self):
        conf = ExecutionConfirmation(confirmed_by="human:jane", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=NOW)
        with pytest.raises(ValidationError, match="execution_confirmation"):
            FidelityTradeTicket(**_ticket_kwargs(status=TicketStatus.AWAITING_HUMAN, execution_confirmation=conf))

    def test_filled_status_with_confirmation_accepted(self):
        conf = ExecutionConfirmation(confirmed_by="human:jane", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=NOW)
        ticket = FidelityTradeTicket(**_ticket_kwargs(status=TicketStatus.FILLED, execution_confirmation=conf))
        assert ticket.status == TicketStatus.FILLED

    def test_is_terminal_true_for_filled_cancelled_rejected_expired(self):
        conf = ExecutionConfirmation(confirmed_by="x", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=1.0, confirmed_at=NOW)
        for status in (TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED):
            ticket = FidelityTradeTicket(**_ticket_kwargs(status=status))
            assert ticket.is_terminal is True
        filled = FidelityTradeTicket(**_ticket_kwargs(status=TicketStatus.FILLED, execution_confirmation=conf))
        assert filled.is_terminal is True

    def test_is_terminal_false_for_non_terminal_statuses(self):
        for status in (
            TicketStatus.PROPOSED, TicketStatus.QUANT_APPROVED, TicketStatus.LLM_REVIEWED,
            TicketStatus.RISK_APPROVED, TicketStatus.AWAITING_HUMAN, TicketStatus.ORDER_ENTERED,
        ):
            ticket = FidelityTradeTicket(**_ticket_kwargs(status=status))
            assert ticket.is_terminal is False

    def test_extra_field_rejected(self):
        payload = {**_ticket_kwargs(), "broker_order_id": "IBKR-123"}
        with pytest.raises(ValidationError):
            FidelityTradeTicket.model_validate(payload)

    def test_all_11_named_states_are_valid_enum_members(self):
        expected = {
            "proposed", "quant_approved", "llm_reviewed", "risk_approved", "awaiting_human",
            "order_entered", "partially_filled", "filled", "cancelled", "rejected", "expired",
        }
        assert {s.value for s in TicketStatus} == expected
