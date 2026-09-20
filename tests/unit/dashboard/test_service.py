"""Tests for src.dashboard.service: the human-execution workflow on top
of a real Risk-Engine-approved FidelityTradeTicket. Every test builds
its scenario from `tests.unit.risk.conftest`'s already-established
builders -- a dashboard opportunity is exactly a Risk-Engine-approved
scenario with a MANUAL broker capability."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.brokers.fidelity import TicketStatus
from src.risk.engine import evaluate_trade_proposal
from src.risk.reason_codes import RiskDecision
from src.dashboard.models import AuditEventType
from src.dashboard.service import (
    DashboardActionError,
    OpportunityNotFoundError,
    cancel_order,
    check_and_apply_staleness,
    copy_fidelity_order,
    get_opportunity,
    mark_order_entered,
    record_fill,
    refresh_price,
    register_opportunity,
    reject_trade,
)
from tests.unit.risk.conftest import EXPIRATION, MD_TS, NOW, build_approved_pcs_scenario, make_contract, make_underlying

from tests.unit.dashboard.conftest import LATER


class TestRegisterOpportunity:
    def test_registers_and_logs_approval(self, registered_opportunity):
        state, record = registered_opportunity
        assert record.trade_id in state.opportunities
        assert record.ticket.status == TicketStatus.AWAITING_HUMAN
        events = state.audit_log.for_trade(record.trade_id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.APPROVAL

    def test_rejects_risk_decision_with_no_ticket(self, dashboard_state, pcs_scenario, manual_caps):
        halted_portfolio = pcs_scenario.portfolio.model_copy(update={"halted": True, "halt_reason": "stop"})
        result = evaluate_trade_proposal(
            pcs_scenario.proposal, halted_portfolio, pcs_scenario.quantitative_analysis,
            pcs_scenario.market_data, manual_caps, limits=pcs_scenario.limits, now=NOW,
        )
        assert result.fidelity_ticket is None
        with pytest.raises(DashboardActionError):
            register_opportunity(
                dashboard_state, proposal=pcs_scenario.proposal, market_data=pcs_scenario.market_data,
                quantitative_analysis=pcs_scenario.quantitative_analysis, devils_advocate_review=None,
                risk_decision=result, now=NOW,
            )


class TestGetOpportunity:
    def test_unknown_trade_id_raises_not_found(self, dashboard_state):
        with pytest.raises(OpportunityNotFoundError):
            get_opportunity(dashboard_state, "does-not-exist")

    def test_known_trade_id_returns_record(self, registered_opportunity):
        state, record = registered_opportunity
        assert get_opportunity(state, record.trade_id) is record


class TestCopyFidelityOrder:
    def test_copy_returns_rendered_text_and_logs_it(self, registered_opportunity):
        state, record = registered_opportunity
        text = copy_fidelity_order(state, record.trade_id, NOW, actor="dipesh")
        assert text.startswith("FIDELITY TRADER+ ORDER")
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.TICKET_COPY]
        assert len(events) == 1

    def test_copy_disabled_once_stale(self, registered_opportunity):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        with pytest.raises(DashboardActionError):
            copy_fidelity_order(state, record.trade_id, much_later, actor="dipesh")
        assert state.opportunities[record.trade_id].ticket.status == TicketStatus.REPRICE_REQUIRED

    def test_copy_disabled_after_order_entered(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError):
            copy_fidelity_order(state, record.trade_id, LATER, actor="dipesh")


class TestStaleness:
    def test_fresh_ticket_unaffected(self, registered_opportunity):
        state, record = registered_opportunity
        result = check_and_apply_staleness(state, record.trade_id, NOW + timedelta(minutes=5))
        assert result.ticket.status == TicketStatus.AWAITING_HUMAN

    def test_stale_ticket_transitions_to_reprice_required(self, registered_opportunity):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        result = check_and_apply_staleness(state, record.trade_id, much_later)
        assert result.ticket.status == TicketStatus.REPRICE_REQUIRED
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.REFRESH]
        assert len(events) == 1

    def test_terminal_ticket_is_never_touched(self, registered_opportunity):
        state, record = registered_opportunity
        reject_trade(state, record.trade_id, reason="no thanks", at=NOW, actor="dipesh")
        much_later = NOW + timedelta(hours=2)
        result = check_and_apply_staleness(state, record.trade_id, much_later)
        assert result.ticket.status == TicketStatus.REJECTED


class TestMarkOrderEntered:
    def test_happy_path(self, registered_opportunity):
        state, record = registered_opportunity
        result = mark_order_entered(state, record.trade_id, actual_limit_entered=0.74, contracts=2, entered_at=LATER, entered_by="dipesh")
        assert result.ticket.status == TicketStatus.ORDER_ENTERED
        assert result.order_entry.actual_limit_entered == 0.74
        assert result.order_entry.contracts == 2
        assert result.order_entry.entered_by == "dipesh"
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.ORDER_ENTERED]
        assert len(events) == 1

    def test_cannot_mark_entered_twice(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.74, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError):
            mark_order_entered(state, record.trade_id, actual_limit_entered=0.74, contracts=2, entered_at=LATER, entered_by="dipesh")

    def test_cannot_mark_entered_while_stale(self, registered_opportunity):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        check_and_apply_staleness(state, record.trade_id, much_later)
        with pytest.raises(DashboardActionError):
            mark_order_entered(state, record.trade_id, actual_limit_entered=0.74, contracts=2, entered_at=much_later, entered_by="dipesh")

    def test_non_positive_contracts_rejected(self, registered_opportunity):
        state, record = registered_opportunity
        with pytest.raises(DashboardActionError):
            mark_order_entered(state, record.trade_id, actual_limit_entered=0.74, contracts=0, entered_at=LATER, entered_by="dipesh")

    def test_non_positive_limit_rejected(self, registered_opportunity):
        state, record = registered_opportunity
        with pytest.raises(DashboardActionError):
            mark_order_entered(state, record.trade_id, actual_limit_entered=-1.0, contracts=2, entered_at=LATER, entered_by="dipesh")


class TestRecordFill:
    def test_full_fill_updates_ticket_and_portfolio(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        fill_at = LATER + timedelta(minutes=2)
        starting_cash = state.portfolio.cash
        result = record_fill(state, record.trade_id, status="FILLED", fill_price=0.76, contracts_filled=2, confirmed_at=fill_at, confirmed_by="dipesh")
        assert result.ticket.status == TicketStatus.FILLED
        assert result.ticket.execution_confirmation is not None
        assert len(state.portfolio.positions) == 1
        assert state.portfolio.cash < starting_cash  # capital_at_risk was committed
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.FILL]
        assert len(events) == 1

    def test_partial_fill_status_and_no_full_position_yet_still_recorded(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        fill_at = LATER + timedelta(minutes=2)
        result = record_fill(state, record.trade_id, status="PARTIALLY_FILLED", fill_price=0.76, contracts_filled=1, confirmed_at=fill_at, confirmed_by="dipesh")
        assert result.ticket.status == TicketStatus.PARTIALLY_FILLED
        assert len(state.portfolio.positions) == 1
        assert state.portfolio.positions[0].contracts == 1
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.PARTIAL_FILL]
        assert len(events) == 1

    def test_mislabeled_partial_that_is_actually_full_becomes_filled(self, registered_opportunity):
        """confirm_fill()'s own arithmetic wins over the caller's label."""
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        fill_at = LATER + timedelta(minutes=2)
        result = record_fill(state, record.trade_id, status="PARTIALLY_FILLED", fill_price=0.76, contracts_filled=2, confirmed_at=fill_at, confirmed_by="dipesh")
        assert result.ticket.status == TicketStatus.FILLED
        events = [e.event_type for e in state.audit_log.for_trade(record.trade_id)]
        assert AuditEventType.FILL in events
        assert AuditEventType.PARTIAL_FILL not in events

    def test_cannot_fill_before_order_entered(self, registered_opportunity):
        state, record = registered_opportunity
        with pytest.raises(DashboardActionError):
            record_fill(state, record.trade_id, status="FILLED", fill_price=0.76, contracts_filled=2, confirmed_at=LATER, confirmed_by="dipesh")

    def test_invalid_status_string_rejected(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError):
            record_fill(state, record.trade_id, status="EXECUTED", fill_price=0.76, contracts_filled=2, confirmed_at=LATER, confirmed_by="dipesh")

    def test_fill_that_would_drive_cash_negative_fails_closed(self, registered_opportunity):
        """Fails closed (a caught, distinct error) rather than silently
        producing an invalid negative-cash Portfolio -- mirrors the
        SY-005 guard `default_portfolio_update_stage` itself uses. Built
        by registering a normal, richly-capitalized ticket, then
        swapping the dashboard's own portfolio for a cash-poor one right
        before the fill is confirmed, so the ticket's own capital_at_risk
        genuinely exceeds what's available."""
        state, record = registered_opportunity
        state.portfolio = state.portfolio.model_copy(update={"cash": 1.0})
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError, match="cash negative"):
            record_fill(state, record.trade_id, status="FILLED", fill_price=0.76, contracts_filled=2, confirmed_at=LATER, confirmed_by="dipesh")


class TestCancelOrder:
    def test_cancel_after_order_entered(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        result = cancel_order(state, record.trade_id, reason="changed my mind", at=LATER + timedelta(minutes=1), actor="dipesh")
        assert result.ticket.status == TicketStatus.CANCELLED
        assert result.cancellation_reason == "changed my mind"
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.CANCELLATION]
        assert len(events) == 1

    def test_cancel_from_awaiting_human(self, registered_opportunity):
        state, record = registered_opportunity
        result = cancel_order(state, record.trade_id, reason="never entered", at=LATER, actor="dipesh")
        assert result.ticket.status == TicketStatus.CANCELLED

    def test_cannot_cancel_a_filled_ticket(self, registered_opportunity):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        record_fill(state, record.trade_id, status="FILLED", fill_price=0.76, contracts_filled=2, confirmed_at=LATER, confirmed_by="dipesh")
        with pytest.raises(DashboardActionError):
            cancel_order(state, record.trade_id, reason="too late", at=LATER, actor="dipesh")


class TestRejectTrade:
    def test_reject_from_awaiting_human(self, registered_opportunity):
        state, record = registered_opportunity
        result = reject_trade(state, record.trade_id, reason="don't like it", at=NOW, actor="dipesh")
        assert result.ticket.status == TicketStatus.REJECTED
        assert result.rejection_reason == "don't like it"
        events = [e for e in state.audit_log.for_trade(record.trade_id) if e.event_type == AuditEventType.REJECTION]
        assert len(events) == 1

    def test_reject_from_reprice_required(self, registered_opportunity):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        check_and_apply_staleness(state, record.trade_id, much_later)
        result = reject_trade(state, record.trade_id, reason="stale, not interested", at=much_later, actor="dipesh")
        assert result.ticket.status == TicketStatus.REJECTED

    def test_cannot_reject_an_already_entered_order(self, registered_opportunity):
        """REJECTED is the pre-entry decision only -- once entered, the
        only way back is CANCELLED."""
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError):
            reject_trade(state, record.trade_id, reason="too late", at=LATER, actor="dipesh")

    def test_cannot_reject_twice(self, registered_opportunity):
        state, record = registered_opportunity
        reject_trade(state, record.trade_id, reason="no", at=NOW, actor="dipesh")
        with pytest.raises(DashboardActionError):
            reject_trade(state, record.trade_id, reason="no again", at=NOW, actor="dipesh")


class TestRefreshPrice:
    def _fresh_chain(self, scenario, as_of):
        underlying = make_underlying(timestamp=as_of - timedelta(minutes=1))
        contracts = [c.model_copy(update={"timestamp": as_of - timedelta(minutes=1)}) for c in scenario.market_data.contracts]
        return scenario.market_data.model_copy(update={"underlying": underlying, "contracts": contracts})

    def test_refresh_with_fresh_data_re_approves_and_regenerates_ticket(self, registered_opportunity, pcs_scenario, manual_caps):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        check_and_apply_staleness(state, record.trade_id, much_later)
        assert state.opportunities[record.trade_id].ticket.status == TicketStatus.REPRICE_REQUIRED

        fresh_chain = self._fresh_chain(pcs_scenario, much_later)
        result = refresh_price(state, record.trade_id, fresh_market_data=fresh_chain, now=much_later, manual_broker_capabilities=manual_caps, actor="dipesh")
        assert result.ticket.status == TicketStatus.AWAITING_HUMAN
        assert result.market_data is fresh_chain
        events = [e.event_type for e in state.audit_log.for_trade(record.trade_id)]
        assert AuditEventType.APPROVAL in events
        assert AuditEventType.REFRESH in events

    def test_refresh_with_still_stale_data_keeps_reprice_required(self, registered_opportunity, pcs_scenario, manual_caps):
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        check_and_apply_staleness(state, record.trade_id, much_later)
        # deliberately still-stale market data (timestamps not advanced)
        result = refresh_price(state, record.trade_id, fresh_market_data=pcs_scenario.market_data, now=much_later, manual_broker_capabilities=manual_caps, actor="dipesh")
        assert result.ticket.status == TicketStatus.REPRICE_REQUIRED

    def test_refresh_ineligible_from_order_entered_status(self, registered_opportunity, pcs_scenario, manual_caps):
        state, record = registered_opportunity
        mark_order_entered(state, record.trade_id, actual_limit_entered=0.75, contracts=2, entered_at=LATER, entered_by="dipesh")
        with pytest.raises(DashboardActionError):
            refresh_price(state, record.trade_id, fresh_market_data=pcs_scenario.market_data, now=LATER, manual_broker_capabilities=manual_caps, actor="dipesh")

    def test_refresh_that_no_longer_passes_risk_engine_rejects_the_ticket(self, registered_opportunity, pcs_scenario, manual_caps):
        """The deterministic Risk Engine's veto is absolute, even on a
        refresh: if it no longer approves at the new price, the ticket
        must be rejected outright, never left showing a stale approval."""
        state, record = registered_opportunity
        much_later = NOW + timedelta(hours=2)
        check_and_apply_staleness(state, record.trade_id, much_later)

        # Fresh (quant can still price it -- bid/ask are real and tight),
        # but now below the Risk Engine's own minimum volume/open-interest
        # liquidity thresholds, which `default_quant_stage` itself doesn't
        # check (MD-002) but `evaluate_trade_proposal` always does.
        underlying = make_underlying(timestamp=much_later - timedelta(minutes=1))
        bad_contracts = [
            c.model_copy(update={"timestamp": much_later - timedelta(minutes=1), "volume": 1, "open_interest": 1})
            for c in pcs_scenario.market_data.contracts
        ]
        bad_chain = pcs_scenario.market_data.model_copy(update={"underlying": underlying, "contracts": bad_contracts})

        result = refresh_price(state, record.trade_id, fresh_market_data=bad_chain, now=much_later, manual_broker_capabilities=manual_caps, actor="dipesh")
        assert result.ticket.status == TicketStatus.REJECTED
        assert result.rejection_reason is not None
        events = [e.event_type for e in state.audit_log.for_trade(record.trade_id)]
        assert AuditEventType.REJECTION in events
