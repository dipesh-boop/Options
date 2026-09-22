"""Part 32: persistent, deduplicated control-loop alerts.

A deliberately separate, portfolio-scoped alert layer from
`src.lifecycle.alerts.Alert` (Part 23) -- reused conventions (immutable,
`resolved`/`resolved_at`, "never raise a duplicate for the same
still-unresolved condition"), never a modification of that frozen Step
22.3 model. The two differ in exactly the dimension Part 32 needs and
Part 23's own `Alert` cannot express: `Alert.trade_id` is mandatory
because every Part 23 alert is about one specific position, but Part
32's own list -- Risk halt, drawdown, provider outage, a stale pending
Fidelity ticket, a new Risk-approved opportunity -- is mostly NOT about
one position at all. `ControlLoopAlert.scope` generalizes `trade_id` to
whatever the alert is actually about: `"portfolio"`, a provider name, a
position_id, a ticket's trade_id, or an opportunity's proposal_id.

Severity (Part 32's exact four-level vocabulary) has no equivalent on
`Alert` either -- every `ControlLoopAlertType` maps to exactly one
severity, decided once here, not left to the caller to improvise per
call site.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from src.portfolio.actions import ControlLoopAction
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot
from src.portfolio.ticket_monitor import TicketMonitorResult
from src.risk.drawdown import DrawdownZone


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class AlertSeverity(str, Enum):
    INFO = "info"
    REVIEW = "review"
    WARNING = "warning"
    CRITICAL = "critical"


class ControlLoopAlertType(str, Enum):
    RISK_HALT = "risk_halt"
    DRAWDOWN_WARNING = "drawdown_warning"
    DRAWDOWN_RISK_REDUCTION = "drawdown_risk_reduction"
    STALE_MARKET_DATA = "stale_market_data"
    PROVIDER_OUTAGE = "provider_outage"
    RATE_LIMIT_CONSTRAINED = "rate_limit_constrained"
    LIFECYCLE_EXIT_REQUIRED = "lifecycle_exit_required"
    PROFIT_TARGET = "profit_target"
    LOSS_THRESHOLD = "loss_threshold"
    DTE_EXIT = "dte_exit"
    ASSIGNMENT_RISK = "assignment_risk"
    EXPIRATION_APPROACHING = "expiration_approaching"
    PENDING_TICKET_STALE = "pending_ticket_stale"
    FILL_RECONCILIATION_REQUIRED = "fill_reconciliation_required"
    DATA_INSUFFICIENT = "data_insufficient"
    NEW_OPPORTUNITY = "new_opportunity"


_ALERT_SEVERITY: dict[ControlLoopAlertType, AlertSeverity] = {
    ControlLoopAlertType.RISK_HALT: AlertSeverity.CRITICAL,
    ControlLoopAlertType.DRAWDOWN_WARNING: AlertSeverity.WARNING,
    ControlLoopAlertType.DRAWDOWN_RISK_REDUCTION: AlertSeverity.CRITICAL,
    ControlLoopAlertType.STALE_MARKET_DATA: AlertSeverity.WARNING,
    ControlLoopAlertType.PROVIDER_OUTAGE: AlertSeverity.CRITICAL,
    ControlLoopAlertType.RATE_LIMIT_CONSTRAINED: AlertSeverity.WARNING,
    ControlLoopAlertType.LIFECYCLE_EXIT_REQUIRED: AlertSeverity.CRITICAL,
    ControlLoopAlertType.PROFIT_TARGET: AlertSeverity.REVIEW,
    ControlLoopAlertType.LOSS_THRESHOLD: AlertSeverity.WARNING,
    ControlLoopAlertType.DTE_EXIT: AlertSeverity.REVIEW,
    ControlLoopAlertType.ASSIGNMENT_RISK: AlertSeverity.WARNING,
    ControlLoopAlertType.EXPIRATION_APPROACHING: AlertSeverity.REVIEW,
    ControlLoopAlertType.PENDING_TICKET_STALE: AlertSeverity.WARNING,
    ControlLoopAlertType.FILL_RECONCILIATION_REQUIRED: AlertSeverity.REVIEW,
    ControlLoopAlertType.DATA_INSUFFICIENT: AlertSeverity.WARNING,
    ControlLoopAlertType.NEW_OPPORTUNITY: AlertSeverity.INFO,
}

PORTFOLIO_SCOPE = "portfolio"

# Which recommended actions (Part 17 vocabulary) map to which alert type
# -- a straight relabeling, same discipline as
# `src.portfolio.actions.action_from_lifecycle_category`: total over
# every action this function is expected to alert on, HOLD/NO_ACTION/
# CLOSE/REDUCE/REVIEW/ROLL_CANDIDATE/ADJUSTMENT_CANDIDATE/CALLED_AWAY/
# RISK_REJECT deliberately absent (no Part 32 alert type fits them, or
# they're already covered by a more specific check elsewhere in this
# module -- e.g. PORTFOLIO_HALT is handled from the kill-switch result
# directly, not from a per-position snapshot).
_ACTION_TO_ALERT_TYPE: dict[ControlLoopAction, ControlLoopAlertType] = {
    ControlLoopAction.EXIT_REQUIRED: ControlLoopAlertType.LIFECYCLE_EXIT_REQUIRED,
    ControlLoopAction.PROFIT_TAKE: ControlLoopAlertType.PROFIT_TARGET,
    ControlLoopAction.TIME_EXIT: ControlLoopAlertType.DTE_EXIT,
    ControlLoopAction.ASSIGNMENT_REVIEW: ControlLoopAlertType.ASSIGNMENT_RISK,
    ControlLoopAction.DATA_INSUFFICIENT: ControlLoopAlertType.DATA_INSUFFICIENT,
}


class ControlLoopAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    scope: str
    alert_type: ControlLoopAlertType
    severity: AlertSeverity
    reason: str
    created_at: datetime
    resolved: bool = False
    resolved_at: datetime | None = None

    _validate_created = field_validator("created_at")(_tz_aware)

    @field_validator("resolved_at")
    @classmethod
    def _resolved_at_tz_aware(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None


def has_unresolved_alert(existing: list[ControlLoopAlert], *, scope: str, alert_type: ControlLoopAlertType) -> bool:
    return any(a.scope == scope and a.alert_type == alert_type and not a.resolved for a in existing)


def raise_alert_if_new(
    existing: list[ControlLoopAlert], *, scope: str, alert_type: ControlLoopAlertType, reason: str, now: datetime
) -> ControlLoopAlert | None:
    """Returns a new alert only if no unresolved alert of the same
    `(scope, alert_type)` already exists -- Part 32's "deduplication/
    state-aware, no repeated duplicates every cycle," the identical
    mechanism `src.lifecycle.alerts.raise_alert_if_new` already
    establishes for per-position alerts."""
    if has_unresolved_alert(existing, scope=scope, alert_type=alert_type):
        return None
    return ControlLoopAlert(
        alert_id=f"CL-ALERT-{uuid.uuid4().hex[:20]}", scope=scope, alert_type=alert_type,
        severity=_ALERT_SEVERITY[alert_type], reason=reason, created_at=now,
    )


def resolve_alert(alert: ControlLoopAlert, *, resolved_at: datetime) -> ControlLoopAlert:
    if alert.resolved:
        return alert
    return alert.model_copy(update={"resolved": True, "resolved_at": resolved_at})


def generate_cycle_alerts(
    *,
    cycle_record: ControlCycleRecord,
    decision_snapshots: list[PortfolioControlDecisionSnapshot],
    drawdown_zone: DrawdownZone,
    existing: list[ControlLoopAlert],
    now: datetime,
    ticket_monitor_result: TicketMonitorResult | None = None,
    rate_limit_degraded: bool = False,
) -> list[ControlLoopAlert]:
    """One control-loop cycle's full Part 32 alert pass. Every call is
    dedup-safe (see `raise_alert_if_new`) -- calling this once per
    cycle, every cycle, never produces a second alert for a condition
    that is still firing from the previous cycle. Returns only the
    NEWLY raised alerts this call actually produced; the caller persists
    them alongside whatever's already stored."""
    new_alerts: list[ControlLoopAlert] = []

    def _raise(scope: str, alert_type: ControlLoopAlertType, reason: str) -> None:
        alert = raise_alert_if_new(existing, scope=scope, alert_type=alert_type, reason=reason, now=now)
        if alert is not None:
            new_alerts.append(alert)
            existing.append(alert)  # so a second condition this same cycle doesn't re-dupe against a stale view

    if cycle_record.halt_state:
        _raise(PORTFOLIO_SCOPE, ControlLoopAlertType.RISK_HALT, "portfolio Risk Engine halt is active")

    if drawdown_zone == DrawdownZone.RISK_REDUCTION:
        _raise(PORTFOLIO_SCOPE, ControlLoopAlertType.DRAWDOWN_RISK_REDUCTION, f"drawdown zone is {drawdown_zone.value}")
    elif drawdown_zone == DrawdownZone.WARNING:
        _raise(PORTFOLIO_SCOPE, ControlLoopAlertType.DRAWDOWN_WARNING, f"drawdown zone is {drawdown_zone.value}")

    if cycle_record.symbols_requested and not cycle_record.symbols_successful:
        _raise(
            cycle_record.provider, ControlLoopAlertType.PROVIDER_OUTAGE,
            f"provider {cycle_record.provider!r} returned no usable data for any of "
            f"{len(cycle_record.symbols_requested)} requested symbol(s) this cycle",
        )
    else:
        for symbol in cycle_record.symbols_failed:
            _raise(symbol, ControlLoopAlertType.STALE_MARKET_DATA, f"no usable market data for {symbol} this cycle")

    if rate_limit_degraded:
        _raise(
            cycle_record.provider, ControlLoopAlertType.RATE_LIMIT_CONSTRAINED,
            f"{cycle_record.provider!r} rate-limit headroom is constrained; lower-priority work is being throttled",
        )

    for snapshot in decision_snapshots:
        if snapshot.position_id is None:
            continue
        alert_type = _ACTION_TO_ALERT_TYPE.get(snapshot.recommended_action)
        if alert_type is not None:
            _raise(snapshot.position_id, alert_type, snapshot.reason_codes[0] if snapshot.reason_codes else snapshot.recommended_action.value)

        # Independent of whatever action fired above -- a position can be
        # both, say, ADJUSTMENT_CANDIDATE (no alert type of its own) AND
        # inside the expiration-approaching window at the same time; this
        # check must never be skipped just because the action above
        # didn't map to an alert type (or already alerted as an exit).
        if (
            snapshot.dte is not None and 0 <= snapshot.dte <= 7
            and alert_type != ControlLoopAlertType.LIFECYCLE_EXIT_REQUIRED
        ):
            _raise(snapshot.position_id, ControlLoopAlertType.EXPIRATION_APPROACHING, f"{snapshot.dte} DTE remaining")

    if ticket_monitor_result is not None:
        for ticket in ticket_monitor_result.repriced_tickets:
            _raise(ticket.trade_id, ControlLoopAlertType.PENDING_TICKET_STALE, "pending Fidelity ticket moved to REPRICE_REQUIRED")

    _no_opportunity_alert_actions = frozenset(
        {ControlLoopAction.RISK_REJECT, ControlLoopAction.NO_ACTION, ControlLoopAction.DATA_INSUFFICIENT, ControlLoopAction.PORTFOLIO_HALT}
    )
    for snapshot in decision_snapshots:
        if snapshot.opportunity_proposal_id is not None and snapshot.recommended_action not in _no_opportunity_alert_actions:
            _raise(
                snapshot.opportunity_proposal_id, ControlLoopAlertType.NEW_OPPORTUNITY,
                snapshot.new_opportunity_comparison or "new Risk-approved opportunity this cycle",
            )

    return new_alerts
