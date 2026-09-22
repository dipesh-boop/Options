"""Part 23: internal lifecycle alerts.

Alerts are a persisted, human-facing signal layer distinct from a
`TriggerFinding` (`src.lifecycle.triggers`) or a `ResolvedAction`
(`src.lifecycle.precedence`) — those drive the deterministic state
machine itself; an `Alert` is the auditable record of "this condition
was flagged" for the dashboard (Part 22) and for after-the-fact review.
**Duplicate alerts for the same still-unresolved condition are never
raised** (Part 23's explicit requirement) — `raise_alert_if_new` checks
the caller-supplied set of that trade's existing alerts before
building a new one, so calling it once per evaluation, every
evaluation, is always safe.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.triggers import TriggerFinding


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class AlertType(str, Enum):
    """Part 23's ten named types verbatim, plus `VOLATILITY_CHANGE`
    (Part 23 says "at minimum" — Part 8's volatility management has no
    home in the literal ten without one). `DTE_EXIT`'s value is the
    literal `"21_dte_exit"` Part 23 names; the member itself is
    renamed only because `21_DTE_EXIT` is not a legal Python
    identifier."""

    PROFIT_TARGET_REACHED = "profit_target_reached"
    LOSS_THRESHOLD_REACHED = "loss_threshold_reached"
    DTE_EXIT = "21_dte_exit"
    DELTA_THRESHOLD = "delta_threshold"
    EARNINGS_APPROACHING = "earnings_approaching"
    ASSIGNMENT_RISK = "assignment_risk"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"
    RISK_LIMIT_BREACH = "risk_limit_breach"
    DATA_STALE = "data_stale"
    REGIME_CHANGE = "regime_change"
    VOLATILITY_CHANGE = "volatility_change"


_TRIGGER_NAME_TO_ALERT_TYPE: dict[str, AlertType] = {
    "profit_target_pct": AlertType.PROFIT_TARGET_REACHED,
    "profit_target_underlying_price": AlertType.PROFIT_TARGET_REACHED,
    "profit_target_option_value": AlertType.PROFIT_TARGET_REACHED,
    "max_loss_pct": AlertType.LOSS_THRESHOLD_REACHED,
    "max_loss_multiple_of_credit": AlertType.LOSS_THRESHOLD_REACHED,
    "underlying_technical_invalidation_price": AlertType.LOSS_THRESHOLD_REACHED,
    "forced_exit_dte": AlertType.DTE_EXIT,
    "management_dte": AlertType.DTE_EXIT,
    "delta_threshold": AlertType.DELTA_THRESHOLD,
    "delta_close_threshold": AlertType.DELTA_THRESHOLD,
    "earnings_exit_days": AlertType.EARNINGS_APPROACHING,
    "assignment_risk_imminent_expiration": AlertType.ASSIGNMENT_RISK,
    "assignment_risk_low_extrinsic": AlertType.ASSIGNMENT_RISK,
    "liquidity_deterioration_threshold": AlertType.LIQUIDITY_DETERIORATION,
    "quote_staleness_limit_minutes": AlertType.DATA_STALE,
    "regime_change_action": AlertType.REGIME_CHANGE,
    "volatility_trigger": AlertType.VOLATILITY_CHANGE,
    "dte": AlertType.DATA_STALE,
    "delta": AlertType.DATA_STALE,
}


def alert_type_for_finding(finding: TriggerFinding) -> AlertType:
    """Every finding maps to exactly one `AlertType`. A `system_data_safety`
    finding always maps to `DATA_STALE` regardless of which specific
    input was missing (Part 23 has no more specific missing-data alert
    type) -- checked first so a DATA_INSUFFICIENT finding on, say, the
    `delta` input is never mis-typed as a `DELTA_THRESHOLD` alert."""
    if finding.category == "system_data_safety":
        return AlertType.DATA_STALE
    return _TRIGGER_NAME_TO_ALERT_TYPE.get(finding.trigger_name, AlertType.RISK_LIMIT_BREACH)


def alert_type_for_resolved_action(resolved: ResolvedAction) -> AlertType | None:
    """The `risk_halt` category has no corresponding `TriggerFinding`
    (it's an external signal merged in by `src.lifecycle.precedence`,
    not produced by `src.lifecycle.triggers`) -- mapped to
    `RISK_LIMIT_BREACH` here. Returns `None` for `hold` (no alert for
    "nothing happened") and for any category already covered by one of
    `resolved.all_findings` (the caller should alert on the findings
    themselves via `alert_type_for_finding`, not double-alert on the
    resolved category too)."""
    if resolved.category == "risk_halt":
        return AlertType.RISK_LIMIT_BREACH
    return None


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    trade_id: str
    alert_type: AlertType
    reason: str
    created_at: datetime
    resolved: bool = False
    resolved_at: datetime | None = None

    _validate_created = field_validator("created_at")(_tz_aware)

    @field_validator("resolved_at")
    @classmethod
    def _resolved_at_tz_aware(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None


def has_unresolved_alert(existing: list[Alert], *, trade_id: str, alert_type: AlertType) -> bool:
    return any(a.trade_id == trade_id and a.alert_type == alert_type and not a.resolved for a in existing)


def raise_alert_if_new(
    existing: list[Alert], *, trade_id: str, alert_type: AlertType, reason: str, now: datetime
) -> Alert | None:
    """Returns a new `Alert` only if no unresolved alert of the same
    `(trade_id, alert_type)` already exists in `existing` — otherwise
    `None`, the concrete mechanism behind Part 23's "avoid repeated
    duplicate alerts for the same unresolved condition." Safe to call
    once per lifecycle evaluation regardless of whether the condition
    is new or has been firing for many evaluations in a row."""
    if has_unresolved_alert(existing, trade_id=trade_id, alert_type=alert_type):
        return None
    return Alert(alert_id=f"ALERT-{uuid.uuid4().hex[:20]}", trade_id=trade_id, alert_type=alert_type, reason=reason, created_at=now)


def resolve_alert(alert: Alert, *, resolved_at: datetime) -> Alert:
    if alert.resolved:
        return alert
    return alert.model_copy(update={"resolved": True, "resolved_at": resolved_at})
