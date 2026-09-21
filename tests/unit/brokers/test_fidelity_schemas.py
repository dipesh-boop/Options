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
        account_alias="Individual Brokerage - Options",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=_legs(),
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
        market_data_timestamp=MD_FRESH,
    )
    base.update(overrides)
    return base


def _ticket_kwargs(**overrides) -> dict:
    base = dict(
        trade_id="TICKET-1",
        account_alias="Individual Brokerage - Options",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=_legs(),
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
        # Step 22 (FS-005 fix): FidelityTradeTicket can only ever be
        # directly constructed at AWAITING_HUMAN -- every other status
        # is reached via model_copy(), exactly like the real
        # transition()/confirm_fill() functions do, never by
        # constructing a new instance directly at that status.
        conf = ExecutionConfirmation(confirmed_by="human:jane", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=NOW)
        base = FidelityTradeTicket(**_ticket_kwargs())
        ticket = base.model_copy(update={"status": TicketStatus.FILLED, "execution_confirmation": conf})
        assert ticket.status == TicketStatus.FILLED

    def test_is_terminal_true_for_filled_cancelled_rejected_expired(self):
        conf = ExecutionConfirmation(confirmed_by="x", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=1.0, confirmed_at=NOW)
        base = FidelityTradeTicket(**_ticket_kwargs())
        for status in (TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED):
            ticket = base.model_copy(update={"status": status})
            assert ticket.is_terminal is True
        filled = base.model_copy(update={"status": TicketStatus.FILLED, "execution_confirmation": conf})
        assert filled.is_terminal is True

    def test_is_terminal_false_for_non_terminal_statuses(self):
        base = FidelityTradeTicket(**_ticket_kwargs())
        for status in (
            TicketStatus.PROPOSED, TicketStatus.QUANT_APPROVED, TicketStatus.LLM_REVIEWED,
            TicketStatus.RISK_APPROVED, TicketStatus.AWAITING_HUMAN, TicketStatus.ORDER_ENTERED,
        ):
            ticket = base.model_copy(update={"status": status})
            assert ticket.is_terminal is False

    def test_extra_field_rejected(self):
        payload = {**_ticket_kwargs(), "broker_order_id": "IBKR-123"}
        with pytest.raises(ValidationError):
            FidelityTradeTicket.model_validate(payload)

    def test_all_12_named_states_are_valid_enum_members(self):
        # 11 original states (Step 8) plus REPRICE_REQUIRED (Step 10):
        # a ticket whose price went stale before a human finished
        # entering it, and must be regenerated before re-attempting.
        expected = {
            "proposed", "quant_approved", "llm_reviewed", "risk_approved", "awaiting_human",
            "order_entered", "partially_filled", "filled", "cancelled", "rejected", "expired",
            "reprice_required",
        }
        assert {s.value for s in TicketStatus} == expected

    def test_net_mid_is_average_of_net_bid_and_net_ask(self):
        ticket = FidelityTradeTicket(**_ticket_kwargs(net_bid=1.30, net_ask=1.40))
        assert ticket.net_mid == pytest.approx(1.35)


class TestNetBidAskValidation:
    def test_bid_above_ask_rejected_on_approved_order(self):
        with pytest.raises(ValidationError, match="net_bid"):
            ApprovedOrder(**_approved_kwargs(net_bid=1.50, net_ask=1.40))

    def test_bid_above_ask_rejected_on_ticket(self):
        with pytest.raises(ValidationError, match="net_bid"):
            FidelityTradeTicket(**_ticket_kwargs(net_bid=1.50, net_ask=1.40))

    def test_bid_equal_ask_accepted(self):
        order = ApprovedOrder(**_approved_kwargs(net_bid=1.35, net_ask=1.35))
        assert order.net_bid == order.net_ask


class TestMinimumAcceptablePriceValidation:
    def test_minimum_acceptable_above_target_rejected_for_credit_order(self):
        # A credit order's target IS the best case; a "minimum
        # acceptable" that's higher than the target is a contradiction.
        with pytest.raises(ValidationError, match="minimum_acceptable_price"):
            ApprovedOrder(**_approved_kwargs(limit_price=1.35, minimum_acceptable_price=1.45, estimated_credit_debit=1.35))

    def test_minimum_acceptable_equal_to_target_accepted(self):
        order = ApprovedOrder(**_approved_kwargs(limit_price=1.35, minimum_acceptable_price=1.35))
        assert order.minimum_acceptable_price == 1.35

    def test_minimum_acceptable_below_target_accepted_for_credit_order(self):
        order = ApprovedOrder(**_approved_kwargs(limit_price=1.35, minimum_acceptable_price=1.25))
        assert order.minimum_acceptable_price == 1.25

    def test_minimum_acceptable_below_target_rejected_for_debit_order(self):
        with pytest.raises(ValidationError, match="minimum_acceptable_price"):
            ApprovedOrder(**_approved_kwargs(limit_price=1.35, minimum_acceptable_price=1.25, estimated_credit_debit=-1.35, net_bid=-1.40, net_ask=-1.30))


class TestCapitalAtRiskValidation:
    def test_capital_at_risk_below_max_loss_rejected(self):
        with pytest.raises(ValidationError, match="capital_at_risk"):
            ApprovedOrder(**_approved_kwargs(max_loss=730.0, capital_at_risk=500.0))

    def test_capital_at_risk_equal_to_max_loss_accepted(self):
        order = ApprovedOrder(**_approved_kwargs(max_loss=730.0, capital_at_risk=730.0))
        assert order.capital_at_risk == 730.0

    def test_capital_at_risk_above_max_loss_accepted(self):
        # e.g. a cash-secured put: capital_at_risk (full collateral) can
        # legitimately exceed max_loss (the worst-case dollar loss).
        order = ApprovedOrder(**_approved_kwargs(max_loss=9750.0, capital_at_risk=10000.0))
        assert order.capital_at_risk == 10000.0


class TestManagementDteValidation:
    def test_management_dte_exceeding_total_dte_rejected(self):
        # timestamp=2026-09-20, expiration=2026-10-16 -> 26 total DTE
        with pytest.raises(ValidationError, match="management_dte"):
            ApprovedOrder(**_approved_kwargs(management_dte=27))

    def test_management_dte_equal_to_total_dte_accepted(self):
        order = ApprovedOrder(**_approved_kwargs(management_dte=26))
        assert order.management_dte == 26

    def test_management_dte_zero_accepted(self):
        order = ApprovedOrder(**_approved_kwargs(management_dte=0))
        assert order.management_dte == 0

    def test_negative_management_dte_rejected(self):
        with pytest.raises(ValidationError):
            ApprovedOrder(**_approved_kwargs(management_dte=-1))


class TestNewFieldsRequired:
    """All the Step 8A additions must actually be required — this isn't
    just a rendering nicety, every quantitative value on the ticket
    must come from Python-supplied data, never a default that papers
    over a missing input."""

    @pytest.mark.parametrize(
        "field_name",
        [
            "account_alias", "minimum_acceptable_price", "net_bid", "net_ask",
            "capital_at_risk", "management_dte",
        ],
    )
    def test_missing_field_rejected_on_approved_order(self, field_name: str):
        kwargs = _approved_kwargs()
        del kwargs[field_name]
        with pytest.raises(ValidationError):
            ApprovedOrder(**kwargs)

    @pytest.mark.parametrize(
        "field_name",
        [
            "account_alias", "minimum_acceptable_price", "net_bid", "net_ask",
            "capital_at_risk", "management_dte",
        ],
    )
    def test_missing_field_rejected_on_ticket(self, field_name: str):
        kwargs = _ticket_kwargs()
        del kwargs[field_name]
        with pytest.raises(ValidationError):
            FidelityTradeTicket(**kwargs)

    def test_time_in_force_defaults_to_day(self):
        kwargs = _approved_kwargs()
        del kwargs["time_in_force"]
        order = ApprovedOrder(**kwargs)
        assert order.time_in_force == "DAY"
