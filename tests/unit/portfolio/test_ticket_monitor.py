"""Tests for `src.portfolio.ticket_monitor` (Step 22.4 Parts 21-22)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.brokers.fidelity import (
    ApprovedOrder,
    ExecutionConfirmation,
    FidelityLegAction,
    FidelityManualProvider,
    FidelityOrderLeg,
    TicketStatus,
    transition,
)
from src.portfolio.ticket_monitor import (
    TicketMonitorAction,
    apply_ticket_monitor_finding,
    evaluate_pending_ticket,
    monitor_pending_tickets,
    record_confirmed_fill,
)
from src.quant.black_scholes import OptionRight

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)


def _ticket():
    approved = ApprovedOrder(
        risk_approval_id="r1", account_alias="acct", ticker="SPY", strategy="put_credit_spread",
        underlying_price=455.0, expiration=EXP,
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=450.0, expiration=EXP, contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=440.0, expiration=EXP, contracts=2),
        ],
        quantity=2, limit_price=3.0, minimum_acceptable_price=2.8, estimated_credit_debit=3.0, net_bid=2.9,
        net_ask=3.1, max_profit=600.0, max_loss=1400.0, breakeven=447.0, capital_at_risk=1400.0,
        return_on_capital=0.43, profit_target=1.5, loss_management_rule="x", DTE_management_rule="y",
        management_dte=21, timestamp=NOW, market_data_timestamp=NOW,
    )
    return FidelityManualProvider().generate_trade_ticket(approved)


class TestEvaluatePendingTicket:
    def test_price_still_supported_is_ok(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=2.85, current_net_ask=3.05, current_market_data_timestamp=NOW)
        assert finding.action == TicketMonitorAction.OK

    def test_stale_market_data_detected(self):
        ticket = _ticket()
        old = NOW - timedelta(minutes=30)
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=2.85, current_net_ask=3.05, current_market_data_timestamp=old)
        assert finding.action == TicketMonitorAction.STALE_MARKET_DATA

    def test_price_drift_beyond_threshold_detected(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=3.3, current_net_ask=3.5, current_market_data_timestamp=NOW)
        assert finding.action == TicketMonitorAction.PRICE_DRIFT
        assert finding.price_drift_pct > 0.10

    def test_target_unreachable_for_credit_trade_when_bid_below_minimum(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=2.5, current_net_ask=2.7, current_market_data_timestamp=NOW)
        assert finding.action == TicketMonitorAction.TARGET_UNREACHABLE

    def test_target_unreachable_checked_before_price_drift(self):
        # A quote that is both >10% drifted AND below minimum_acceptable_price
        # must report the more specific/severe TARGET_UNREACHABLE, not PRICE_DRIFT.
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=1.5, current_net_ask=1.7, current_market_data_timestamp=NOW)
        assert finding.action == TicketMonitorAction.TARGET_UNREACHABLE

    def test_missing_quote_reports_no_current_quote(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=None, current_net_ask=None, current_market_data_timestamp=None)
        assert finding.action == TicketMonitorAction.NO_CURRENT_QUOTE

    def test_debit_trade_uses_ask_side_for_unreachable_check(self):
        approved = ApprovedOrder(
            risk_approval_id="r2", account_alias="acct", ticker="SPY", strategy="bull_call_spread",
            underlying_price=455.0, expiration=EXP,
            legs=[
                FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.CALL, strike=455.0, expiration=EXP, contracts=1),
                FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.CALL, strike=465.0, expiration=EXP, contracts=1),
            ],
            quantity=1, limit_price=3.0, minimum_acceptable_price=3.2, estimated_credit_debit=-3.0, net_bid=2.9,
            net_ask=3.1, max_profit=700.0, max_loss=300.0, breakeven=458.0, capital_at_risk=300.0,
            return_on_capital=2.3, profit_target=0.5, loss_management_rule="x", DTE_management_rule="y",
            management_dte=21, timestamp=NOW, market_data_timestamp=NOW,
        )
        ticket = FidelityManualProvider().generate_trade_ticket(approved)
        # ask rose above minimum_acceptable_price=3.2 -> unreachable for a net-debit trade
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=3.3, current_net_ask=3.5, current_market_data_timestamp=NOW)
        assert finding.action == TicketMonitorAction.TARGET_UNREACHABLE


class TestApplyTicketMonitorFinding:
    def test_ok_finding_leaves_ticket_unchanged(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=2.85, current_net_ask=3.05, current_market_data_timestamp=NOW)
        result = apply_ticket_monitor_finding(ticket, finding, at=NOW)
        assert result.status == TicketStatus.AWAITING_HUMAN

    def test_bad_finding_transitions_to_reprice_required(self):
        ticket = _ticket()
        finding = evaluate_pending_ticket(ticket, as_of=NOW, current_net_bid=3.3, current_net_ask=3.5, current_market_data_timestamp=NOW)
        result = apply_ticket_monitor_finding(ticket, finding, at=NOW)
        assert result.status == TicketStatus.REPRICE_REQUIRED


class TestMonitorPendingTickets:
    def test_reprices_bad_tickets_and_reports_findings(self):
        ticket = _ticket()
        result = monitor_pending_tickets([ticket], as_of=NOW, current_quotes_by_trade_id={ticket.trade_id: (3.3, 3.5, NOW)})
        assert result.reprice_required_count == 1
        assert result.repriced_tickets[0].status == TicketStatus.REPRICE_REQUIRED

    def test_non_pending_ticket_status_is_skipped_entirely(self):
        ticket = transition(_ticket(), TicketStatus.CANCELLED, at=NOW)
        result = monitor_pending_tickets([ticket], as_of=NOW, current_quotes_by_trade_id={})
        assert result.findings == () and result.repriced_tickets == ()

    def test_one_bad_ticket_does_not_block_evaluating_others(self):
        good = _ticket()
        bad_approved = ApprovedOrder(
            risk_approval_id="r3", account_alias="acct", ticker="QQQ", strategy="cash_secured_put",
            underlying_price=380.0, expiration=EXP,
            legs=[FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=370.0, expiration=EXP, contracts=1)],
            quantity=1, limit_price=2.0, minimum_acceptable_price=1.8, estimated_credit_debit=2.0, net_bid=1.9,
            net_ask=2.1, max_profit=200.0, max_loss=36800.0, breakeven=368.0, capital_at_risk=36800.0,
            return_on_capital=0.005, profit_target=1.5, loss_management_rule="x", DTE_management_rule="y",
            management_dte=21, timestamp=NOW, market_data_timestamp=NOW,
        )
        bad = FidelityManualProvider().generate_trade_ticket(bad_approved)
        result = monitor_pending_tickets(
            [good, bad], as_of=NOW,
            current_quotes_by_trade_id={good.trade_id: (2.85, 3.05, NOW)},  # bad has no quote entry at all
        )
        findings_by_trade = {f.trade_id: f for f in result.findings}
        assert findings_by_trade[good.trade_id].action == TicketMonitorAction.OK
        assert findings_by_trade[bad.trade_id].action == TicketMonitorAction.NO_CURRENT_QUOTE


class TestRecordConfirmedFill:
    def test_delegates_to_confirm_fill(self):
        ticket = transition(_ticket(), TicketStatus.ORDER_ENTERED, at=NOW)
        confirmation = ExecutionConfirmation(
            confirmed_by="dipesh", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=3.0, confirmed_at=NOW,
        )
        filled = record_confirmed_fill(ticket, confirmation)
        assert filled.status == TicketStatus.FILLED
        assert filled.execution_confirmation == confirmation
