"""Tests for src.dashboard.models: the audit log's append-only
guarantee, and OrderEntryRecord/DashboardState construction."""
from __future__ import annotations

from datetime import datetime, timezone

from src.dashboard.models import AuditEvent, AuditEventType, AuditLog, DashboardState, OrderEntryRecord
from tests.unit.risk.conftest import build_approved_pcs_scenario

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


def _event(trade_id="t1", event_type=AuditEventType.REFRESH) -> AuditEvent:
    return AuditEvent(event_id="e1", trade_id=trade_id, event_type=event_type, at=NOW, actor="dipesh", detail="x")


class TestAuditLog:
    def test_empty_log_returns_no_events(self):
        log = AuditLog()
        assert log.all() == []

    def test_record_then_all_returns_it(self):
        log = AuditLog()
        event = _event()
        log.record(event)
        assert log.all() == [event]

    def test_all_returns_a_copy_not_the_internal_list(self):
        log = AuditLog()
        log.record(_event())
        snapshot = log.all()
        snapshot.append(_event(trade_id="intruder"))
        assert len(log.all()) == 1  # the log itself is unaffected

    def test_for_trade_filters_by_trade_id(self):
        log = AuditLog()
        log.record(_event(trade_id="t1"))
        log.record(_event(trade_id="t2"))
        log.record(_event(trade_id="t1"))
        assert len(log.for_trade("t1")) == 2
        assert len(log.for_trade("t2")) == 1
        assert len(log.for_trade("unknown")) == 0

    def test_events_recorded_in_order(self):
        log = AuditLog()
        log.record(_event(event_type=AuditEventType.REFRESH))
        log.record(_event(event_type=AuditEventType.APPROVAL))
        log.record(_event(event_type=AuditEventType.REJECTION))
        assert [e.event_type for e in log.all()] == [AuditEventType.REFRESH, AuditEventType.APPROVAL, AuditEventType.REJECTION]

    def test_audit_log_has_no_delete_or_edit_method(self):
        """Structural proof: nothing in this class can remove or alter a
        previously-recorded event."""
        assert not hasattr(AuditLog, "delete")
        assert not hasattr(AuditLog, "remove")
        assert not hasattr(AuditLog, "clear")
        assert not hasattr(AuditLog, "update")


class TestOrderEntryRecord:
    def test_construction(self):
        record = OrderEntryRecord(actual_limit_entered=1.30, contracts=2, entered_at=NOW, entered_by="dipesh")
        assert record.actual_limit_entered == 1.30
        assert record.contracts == 2
        assert record.entered_by == "dipesh"


class TestDashboardState:
    def test_starts_with_no_opportunities_and_empty_audit_log(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        assert state.opportunities == {}
        assert state.audit_log.all() == []

    def test_optional_greeks_and_pnl_default_to_none_not_fabricated(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        assert state.portfolio_net_delta is None
        assert state.portfolio_net_theta is None
        assert state.portfolio_net_vega is None
        assert state.daily_pnl is None
        assert state.ytd_return_pct is None
