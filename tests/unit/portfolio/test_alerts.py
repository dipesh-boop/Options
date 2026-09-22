"""Tests for `src.portfolio.alerts` (Step 22.4 Part 32)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.portfolio.actions import ControlLoopAction
from src.portfolio.alerts import (
    PORTFOLIO_SCOPE,
    AlertSeverity,
    ControlLoopAlertType,
    generate_cycle_alerts,
    has_unresolved_alert,
    raise_alert_if_new,
    resolve_alert,
)
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot
from src.risk.drawdown import DrawdownZone

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _cycle(**overrides) -> ControlCycleRecord:
    fields = dict(
        cycle_id="c1", started_at=NOW, market_open=True, provider="tradier", provider_health_status="healthy",
        symbols_requested=("SPY", "QQQ"), symbols_successful=("SPY",), symbols_failed=("QQQ",), halt_state=False,
    )
    fields.update(overrides)
    return ControlCycleRecord(**fields)


def _snapshot(**overrides) -> PortfolioControlDecisionSnapshot:
    fields = dict(
        cycle_id="c1", timestamp=NOW, is_trading_day=True, is_market_open=True, provider="tradier",
        provider_health_status="healthy", portfolio_nav=100000.0, portfolio_cash=80000.0,
        portfolio_deployed_pct=0.2, portfolio_drawdown_pct=0.02, recommended_action=ControlLoopAction.HOLD,
    )
    fields.update(overrides)
    return PortfolioControlDecisionSnapshot(**fields)


class TestRaiseAlertIfNew:
    def test_raises_a_new_alert_when_none_exists(self):
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        assert alert is not None and alert.resolved is False

    def test_dedup_returns_none_for_unresolved_duplicate(self):
        existing = [raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)]
        assert raise_alert_if_new(existing, scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="y", now=NOW) is None

    def test_raises_again_after_prior_one_resolved(self):
        a1 = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        resolved = resolve_alert(a1, resolved_at=NOW + timedelta(minutes=1))
        a2 = raise_alert_if_new([resolved], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="z", now=NOW)
        assert a2 is not None

    def test_different_scope_is_not_a_duplicate(self):
        existing = [raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)]
        assert raise_alert_if_new(existing, scope="p2", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="y", now=NOW) is not None

    def test_different_alert_type_same_scope_is_not_a_duplicate(self):
        existing = [raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)]
        assert raise_alert_if_new(existing, scope="p1", alert_type=ControlLoopAlertType.DTE_EXIT, reason="y", now=NOW) is not None


class TestResolveAlert:
    def test_flips_resolved_and_sets_timestamp(self):
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        resolved = resolve_alert(alert, resolved_at=NOW + timedelta(minutes=1))
        assert resolved.resolved is True and resolved.resolved_at == NOW + timedelta(minutes=1)

    def test_already_resolved_is_a_no_op(self):
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        resolved = resolve_alert(alert, resolved_at=NOW + timedelta(minutes=1))
        resolved_again = resolve_alert(resolved, resolved_at=NOW + timedelta(minutes=2))
        assert resolved_again.resolved_at == resolved.resolved_at


class TestGenerateCycleAlerts:
    def test_stale_symbol_produces_stale_market_data_alert(self):
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        assert ("QQQ", ControlLoopAlertType.STALE_MARKET_DATA) in {(a.scope, a.alert_type) for a in alerts}

    def test_total_provider_failure_produces_provider_outage_not_per_symbol(self):
        cyc = _cycle(symbols_successful=(), symbols_failed=("SPY", "QQQ"))
        alerts = generate_cycle_alerts(cycle_record=cyc, decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        types = {(a.scope, a.alert_type) for a in alerts}
        assert ("tradier", ControlLoopAlertType.PROVIDER_OUTAGE) in types
        assert not any(t[1] == ControlLoopAlertType.STALE_MARKET_DATA for t in types)

    def test_exit_required_action_produces_lifecycle_exit_required_alert(self):
        snap = _snapshot(position_id="p1", recommended_action=ControlLoopAction.EXIT_REQUIRED, reason_codes=("hard loss",))
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[snap], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        assert ("p1", ControlLoopAlertType.LIFECYCLE_EXIT_REQUIRED) in {(a.scope, a.alert_type) for a in alerts}

    def test_expiration_approaching_fires_independent_of_mapped_action(self):
        # HOLD has no _ACTION_TO_ALERT_TYPE entry -- the DTE check must
        # still fire on its own, not be skipped because HOLD mapped to
        # nothing.
        snap = _snapshot(position_id="p2", recommended_action=ControlLoopAction.HOLD, dte=3)
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[snap], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        assert ("p2", ControlLoopAlertType.EXPIRATION_APPROACHING) in {(a.scope, a.alert_type) for a in alerts}

    def test_far_dte_does_not_trigger_expiration_approaching(self):
        snap = _snapshot(position_id="p2", recommended_action=ControlLoopAction.HOLD, dte=30)
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[snap], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        assert not any(a.alert_type == ControlLoopAlertType.EXPIRATION_APPROACHING for a in alerts)

    def test_portfolio_halt_produces_risk_halt_alert(self):
        alerts = generate_cycle_alerts(cycle_record=_cycle(halt_state=True), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        assert any(a.scope == PORTFOLIO_SCOPE and a.alert_type == ControlLoopAlertType.RISK_HALT and a.severity == AlertSeverity.CRITICAL for a in alerts)

    def test_drawdown_zones_map_to_correct_alert_and_severity(self):
        warn = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.WARNING, existing=[], now=NOW)
        assert any(a.alert_type == ControlLoopAlertType.DRAWDOWN_WARNING and a.severity == AlertSeverity.WARNING for a in warn)

        reduction = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.RISK_REDUCTION, existing=[], now=NOW)
        assert any(a.alert_type == ControlLoopAlertType.DRAWDOWN_RISK_REDUCTION and a.severity == AlertSeverity.CRITICAL for a in reduction)

    def test_new_opportunity_alert_only_for_actionable_recommendation(self):
        approved = _snapshot(opportunity_proposal_id="scan-1", recommended_action=ControlLoopAction.PROFIT_TAKE)
        rejected = _snapshot(opportunity_proposal_id="scan-2", recommended_action=ControlLoopAction.RISK_REJECT)
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[approved, rejected], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW)
        types = {(a.scope, a.alert_type) for a in alerts}
        assert ("scan-1", ControlLoopAlertType.NEW_OPPORTUNITY) in types
        assert ("scan-2", ControlLoopAlertType.NEW_OPPORTUNITY) not in types

    def test_dedup_across_repeated_cycles(self):
        existing: list = []
        first = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=existing, now=NOW)
        assert len(first) > 0
        second = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=existing, now=NOW + timedelta(minutes=5))
        assert second == []

    def test_ticket_monitor_reprice_produces_pending_ticket_stale_alert(self):
        from datetime import date

        from src.brokers.fidelity import (
            ApprovedOrder, FidelityLegAction, FidelityManualProvider, FidelityOrderLeg, transition, TicketStatus,
        )
        from src.portfolio.ticket_monitor import TicketMonitorResult
        from src.quant.black_scholes import OptionRight

        approved = ApprovedOrder(
            risk_approval_id="r1", account_alias="acct", ticker="SPY", strategy="cash_secured_put",
            underlying_price=455.0, expiration=date(2026, 10, 23),
            legs=[FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=450.0, expiration=date(2026, 10, 23), contracts=1)],
            quantity=1, limit_price=3.0, minimum_acceptable_price=2.8, estimated_credit_debit=3.0, net_bid=2.9,
            net_ask=3.1, max_profit=300.0, max_loss=45000.0, breakeven=447.0, capital_at_risk=45000.0,
            return_on_capital=0.006, profit_target=1.5, loss_management_rule="x", DTE_management_rule="y",
            management_dte=21, timestamp=NOW, market_data_timestamp=NOW,
        )
        ticket = FidelityManualProvider().generate_trade_ticket(approved)
        repriced = transition(ticket, TicketStatus.REPRICE_REQUIRED, at=NOW)
        result = TicketMonitorResult(findings=(), repriced_tickets=(repriced,))
        alerts = generate_cycle_alerts(
            cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW,
            ticket_monitor_result=result,
        )
        assert any(a.scope == ticket.trade_id and a.alert_type == ControlLoopAlertType.PENDING_TICKET_STALE for a in alerts)

    def test_rate_limit_degraded_produces_alert(self):
        alerts = generate_cycle_alerts(cycle_record=_cycle(), decision_snapshots=[], drawdown_zone=DrawdownZone.NORMAL, existing=[], now=NOW, rate_limit_degraded=True)
        assert any(a.alert_type == ControlLoopAlertType.RATE_LIMIT_CONSTRAINED for a in alerts)


class TestHasUnresolvedAlert:
    def test_true_for_matching_unresolved(self):
        existing = [raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)]
        assert has_unresolved_alert(existing, scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET) is True

    def test_false_once_resolved(self):
        a = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        resolved = resolve_alert(a, resolved_at=NOW + timedelta(minutes=1))
        assert has_unresolved_alert([resolved], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET) is False
