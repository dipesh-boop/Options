"""Part 23: internal lifecycle alerts, including duplicate-alert
prevention for an unresolved condition."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.lifecycle.alerts import (
    AlertType,
    alert_type_for_finding,
    alert_type_for_resolved_action,
    has_unresolved_alert,
    raise_alert_if_new,
    resolve_alert,
)
from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.state import PositionLifecycleState as S
from src.lifecycle.triggers import TriggerFinding

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestAlertTypeMapping:
    def test_profit_target_finding_maps_correctly(self):
        f = TriggerFinding(trigger_name="profit_target_pct", category="profit_target", target_state=S.PROFIT_TARGET_REACHED, mandatory=False, reason="x")
        assert alert_type_for_finding(f) == AlertType.PROFIT_TARGET_REACHED

    def test_data_safety_finding_always_maps_to_data_stale(self):
        f = TriggerFinding(trigger_name="delta", category="system_data_safety", target_state=S.DATA_INSUFFICIENT, mandatory=True, reason="x")
        assert alert_type_for_finding(f) == AlertType.DATA_STALE

    def test_dte_finding_maps_to_dte_exit(self):
        f = TriggerFinding(trigger_name="forced_exit_dte", category="time_exit", target_state=S.TIME_EXIT_TRIGGERED, mandatory=True, reason="x")
        assert alert_type_for_finding(f) == AlertType.DTE_EXIT

    def test_regime_finding_maps_to_regime_change(self):
        f = TriggerFinding(trigger_name="regime_change_action", category="delta_volatility_review", target_state=S.REGIME_CHANGE_TRIGGERED, mandatory=True, reason="x")
        assert alert_type_for_finding(f) == AlertType.REGIME_CHANGE

    def test_risk_halt_resolved_action_maps_to_risk_limit_breach(self):
        ra = ResolvedAction(category="risk_halt", target_state=S.RISK_EXIT_REQUIRED, mandatory=True, reason="x", winning_trigger_names=(), all_findings=())
        assert alert_type_for_resolved_action(ra) == AlertType.RISK_LIMIT_BREACH

    def test_hold_resolved_action_has_no_alert(self):
        ra = ResolvedAction(category="hold", target_state=S.ACTIVE, mandatory=False, reason="x", winning_trigger_names=(), all_findings=())
        assert alert_type_for_resolved_action(ra) is None


class TestDuplicatePrevention:
    def test_first_alert_is_raised(self):
        alert = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="60%", now=T0)
        assert alert is not None

    def test_second_unresolved_alert_of_same_type_is_suppressed(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="60%", now=T0)
        second = raise_alert_if_new([first], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="65%", now=T0 + timedelta(hours=1))
        assert second is None

    def test_different_alert_type_is_not_suppressed(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        second = raise_alert_if_new([first], trade_id="T1", alert_type=AlertType.LOSS_THRESHOLD_REACHED, reason="x", now=T0)
        assert second is not None

    def test_different_trade_id_is_not_suppressed(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        second = raise_alert_if_new([first], trade_id="T2", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        assert second is not None

    def test_new_alert_allowed_once_prior_one_resolved(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        resolved = resolve_alert(first, resolved_at=T0 + timedelta(days=1))
        third = raise_alert_if_new([resolved], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="fired again", now=T0 + timedelta(days=2))
        assert third is not None

    def test_has_unresolved_alert_helper(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        assert has_unresolved_alert([first], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED)
        resolved = resolve_alert(first, resolved_at=T0)
        assert not has_unresolved_alert([resolved], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED)


class TestResolveAlert:
    def test_resolve_is_idempotent(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        once = resolve_alert(first, resolved_at=T0 + timedelta(days=1))
        twice = resolve_alert(once, resolved_at=T0 + timedelta(days=2))
        assert once.resolved_at == twice.resolved_at

    def test_original_never_mutated(self):
        first = raise_alert_if_new([], trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", now=T0)
        resolve_alert(first, resolved_at=T0 + timedelta(days=1))
        assert first.resolved is False
