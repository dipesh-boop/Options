"""Tests for src.dashboard.ticket_format: the exact COPY FIDELITY ORDER
text template Step 18 specifies."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.fidelity import ApprovedOrder, FidelityLegAction, FidelityManualProvider, FidelityOrderLeg
from src.data.option_chain import OptionRight
from src.dashboard.ticket_format import render_dashboard_order_text

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)


def _approved_order(**overrides) -> ApprovedOrder:
    base = dict(
        risk_approval_id="risk-1", account_alias="OPTIONS_ACCOUNT", ticker="SPY", strategy="PUT CREDIT SPREAD",
        underlying_price=628.5, expiration=date(2026, 10, 16),
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=date(2026, 10, 16), contracts=2),
        ],
        quantity=2, limit_price=1.35, minimum_acceptable_price=1.25, time_in_force="DAY",
        estimated_credit_debit=1.35, net_bid=1.30, net_ask=1.40, max_profit=270.0, max_loss=730.0, breakeven=618.65,
        capital_at_risk=730.0, return_on_capital=270 / 730, profit_target=0.68,
        loss_management_rule="close at 2x credit", DTE_management_rule="manage at 21 dte", management_dte=21,
        timestamp=NOW, market_data_timestamp=MD_TS,
    )
    base.update(overrides)
    return ApprovedOrder(**base)


def _ticket(**overrides):
    return FidelityManualProvider().generate_trade_ticket(_approved_order(**overrides))


class TestCreditSpreadTemplate:
    """Matches Step 18's own worked example exactly."""

    def setup_method(self):
        self.text = render_dashboard_order_text(_ticket())

    def test_starts_with_header(self):
        assert self.text.startswith("FIDELITY TRADER+ ORDER")

    def test_ticker_present(self):
        assert "\nSPY\n" in self.text

    def test_strategy_present(self):
        assert "\nPUT CREDIT SPREAD\n" in self.text

    def test_expiration_formatted_mm_dd_yyyy(self):
        assert "EXPIRATION:\n10/16/2026" in self.text

    def test_sell_to_open_leg(self):
        assert "SELL TO OPEN:\n2 × SPY 620 PUT" in self.text

    def test_buy_to_open_leg(self):
        assert "BUY TO OPEN:\n2 × SPY 615 PUT" in self.text

    def test_order_type_net_credit(self):
        assert "ORDER TYPE:\nNET CREDIT LIMIT" in self.text

    def test_target(self):
        assert "TARGET:\n$1.35" in self.text

    def test_do_not_enter_below_for_credit(self):
        assert "DO NOT ENTER BELOW:\n$1.25" in self.text

    def test_time_in_force(self):
        assert "TIME IN FORCE:\nDAY" in self.text

    def test_quote_section(self):
        assert "Quote:\nBid $1.30 / Ask $1.40 / Mid $1.35" in self.text

    def test_quote_timestamp_section(self):
        assert "Quote timestamp:\n2026-09-20 13:55:00 UTC" in self.text


class TestDebitOrderTemplate:
    def test_order_type_net_debit(self):
        ticket = _ticket(
            estimated_credit_debit=-1.35, limit_price=1.35, minimum_acceptable_price=1.45,
            net_bid=1.30, net_ask=1.40,
        )
        text = render_dashboard_order_text(ticket)
        assert "ORDER TYPE:\nNET DEBIT LIMIT" in text

    def test_do_not_enter_above_for_debit(self):
        ticket = _ticket(
            estimated_credit_debit=-1.35, limit_price=1.35, minimum_acceptable_price=1.45,
            net_bid=1.30, net_ask=1.40,
        )
        text = render_dashboard_order_text(ticket)
        assert "DO NOT ENTER ABOVE:\n$1.45" in text


class TestSingleLegTemplate:
    def test_cash_secured_put_single_leg(self):
        approved = _approved_order(
            strategy="CASH SECURED PUT",
            legs=[FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=95.0, expiration=date(2026, 10, 16), contracts=1)],
            quantity=1, limit_price=2.00, minimum_acceptable_price=1.80, estimated_credit_debit=2.00,
            net_bid=1.95, net_ask=2.05, max_profit=200.0, max_loss=9300.0, breakeven=93.0,
            capital_at_risk=9500.0, return_on_capital=200 / 9500,
        )
        text = render_dashboard_order_text(FidelityManualProvider().generate_trade_ticket(approved))
        assert "SELL TO OPEN:\n1 × SPY 95 PUT" in text
        assert "CASH SECURED PUT" in text


class TestNeverContainsForbiddenContent:
    """No credential field of any kind ever appears in the rendered
    text this module hands to a browser's clipboard."""

    def test_no_password_username_mfa_or_cookie_strings(self):
        text = render_dashboard_order_text(_ticket())
        lowered = text.lower()
        for forbidden in ("password", "username", "mfa", "cookie", "session_token"):
            assert forbidden not in lowered
