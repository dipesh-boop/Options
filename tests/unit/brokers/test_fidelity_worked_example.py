"""The exact worked example from the task spec: SPY put credit spread,
sell 2x 620P / buy 2x 615P, credit 1.35. Cross-checks the example's own
numbers (max profit $270, max loss $730, breakeven $618.65) against
src.quant.expected_value's put_credit_spread formulas — proving the
example is internally consistent with the platform's already-tested
quant engine, not just internally consistent with itself.

Note: fidelity.py itself has no dependency on src.quant (see its module
docstring) — this test file is allowed to import both to verify they
agree; that's not a violation of the module boundary, since the
boundary is about production code, not test code.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.fidelity import (
    ApprovedOrder,
    FidelityLegAction,
    FidelityManualProvider,
    FidelityOrderLeg,
    TicketStatus,
    render_ticket_text,
)
from src.data.option_chain import OptionRight
from src.quant.expected_value import (
    put_credit_spread_breakeven,
    put_credit_spread_max_loss,
    put_credit_spread_max_profit,
)

SHORT_STRIKE = 620.0
LONG_STRIKE = 615.0
CREDIT = 1.35
CONTRACTS = 2
EXPIRATION = date(2026, 10, 16)
NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MARKET_DATA_TIME = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)


class TestExampleNumbersMatchTheQuantEngine:
    def test_max_profit_matches_quant_engine(self):
        assert put_credit_spread_max_profit(CREDIT, CONTRACTS) == pytest.approx(270.0)

    def test_max_loss_matches_quant_engine(self):
        assert put_credit_spread_max_loss(SHORT_STRIKE, LONG_STRIKE, CREDIT, CONTRACTS) == pytest.approx(730.0)

    def test_breakeven_matches_quant_engine(self):
        assert put_credit_spread_breakeven(SHORT_STRIKE, CREDIT) == pytest.approx(618.65)


def _build_approved_order() -> ApprovedOrder:
    max_profit = put_credit_spread_max_profit(CREDIT, CONTRACTS)
    max_loss = put_credit_spread_max_loss(SHORT_STRIKE, LONG_STRIKE, CREDIT, CONTRACTS)
    breakeven = put_credit_spread_breakeven(SHORT_STRIKE, CREDIT)
    return ApprovedOrder(
        risk_approval_id="risk-approval-spy-pcs-001",
        account_alias="Individual Brokerage - Options",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=EXPIRATION,
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=SHORT_STRIKE, expiration=EXPIRATION, contracts=CONTRACTS),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=LONG_STRIKE, expiration=EXPIRATION, contracts=CONTRACTS),
        ],
        quantity=CONTRACTS,
        limit_price=CREDIT,
        minimum_acceptable_price=1.25,
        time_in_force="DAY",
        estimated_credit_debit=CREDIT,
        net_bid=1.30,
        net_ask=1.40,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        capital_at_risk=max_loss,
        return_on_capital=max_profit / max_loss,
        profit_target=0.68,  # buy the spread back near $0.68, per the example
        loss_management_rule="Close or roll if loss approaches 2x the credit received.",
        DTE_management_rule="Review/close/roll according to strategy rules at 21 DTE.",
        management_dte=21,
        timestamp=NOW,
        market_data_timestamp=MARKET_DATA_TIME,
    )


class TestGeneratedTicketMatchesTheExample:
    def test_ticket_carries_the_exact_example_numbers(self):
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order(), trade_id="SPY-PCS-001")
        assert ticket.max_profit == pytest.approx(270.0)
        assert ticket.max_loss == pytest.approx(730.0)
        assert ticket.breakeven == pytest.approx(618.65)
        assert ticket.limit_price == pytest.approx(1.35)
        assert ticket.status == TicketStatus.AWAITING_HUMAN

    def test_ticket_legs_match_the_example(self):
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        sell_leg = next(leg for leg in ticket.legs if leg.action == FidelityLegAction.SELL_TO_OPEN)
        buy_leg = next(leg for leg in ticket.legs if leg.action == FidelityLegAction.BUY_TO_OPEN)
        assert sell_leg.strike == 620.0
        assert sell_leg.contracts == 2
        assert buy_leg.strike == 615.0
        assert buy_leg.contracts == 2

    def test_rendered_text_contains_every_key_figure_from_the_example(self):
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        text = render_ticket_text(ticket)

        assert "Individual Brokerage - Options" in text
        assert "SPY" in text
        assert "PUT CREDIT SPREAD" in text
        assert "10/16/2026" in text
        assert "LEG 1:" in text
        assert "SELL TO OPEN" in text
        assert "620 PUT" in text
        assert "LEG 2:" in text
        assert "BUY TO OPEN" in text
        assert "615 PUT" in text
        assert "2 contracts" in text
        assert "NET CREDIT" in text
        assert "$1.35" in text  # target limit
        assert "$1.25" in text  # minimum acceptable
        assert "DAY" in text  # time in force
        assert "$1.30" in text  # current net bid
        assert "$270" in text  # max profit
        assert "$730" in text  # max loss / capital at risk
        assert "$618.65" in text  # breakeven
        assert "$0.68" in text  # profit target
        assert "37.0%" in text  # return on capital
        assert "21" in text  # management DTE
        assert "AWAITING HUMAN EXECUTION" in text

    def test_rendered_text_never_claims_the_order_was_placed(self):
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        text = render_ticket_text(ticket).upper()
        for forbidden_phrase in ("ORDER PLACED", "ORDER SUBMITTED", "EXECUTED", "FILLED"):
            assert forbidden_phrase not in text

    def test_risk_approved_alone_never_appears_as_a_ticket_status(self):
        # "Risk-approved does not mean executed": generate_trade_ticket's
        # output status is always AWAITING_HUMAN, never anything implying
        # execution, regardless of how thoroughly the order was approved
        # upstream.
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        assert ticket.status not in (TicketStatus.FILLED, TicketStatus.PARTIALLY_FILLED, TicketStatus.ORDER_ENTERED)


class TestCopyFidelityOrderText:
    """The 'COPY FIDELITY ORDER' functionality for the future dashboard
    — this only ever produces text; clipboard access is a frontend
    concern handled outside this backend function."""

    def test_copy_text_matches_render_ticket_text_exactly(self):
        from src.brokers.fidelity import copy_fidelity_order_text

        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        assert copy_fidelity_order_text(ticket) == render_ticket_text(ticket)

    def test_copy_text_is_a_plain_string_with_no_side_effects(self):
        from src.brokers.fidelity import copy_fidelity_order_text

        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_build_approved_order())
        result = copy_fidelity_order_text(ticket)
        assert isinstance(result, str)
        assert "ACCOUNT:" in result
