"""The validation protocol's alert list: named, deterministic conditions
that should draw a human's attention immediately, rather than waiting
for a weekly review or a 30/60/90-day checkpoint to surface them.

Every alert this module can produce is named in `AlertCode` — a closed
enum, not a free-text category, so "what can this protocol alert on" is
answerable by reading one place. Drawdown thresholds are read from the
live `src.risk.limits.RiskLimitsConfig`, never redeclared here — see
`config/validation.yaml`'s own module docstring for why.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from src.risk.limits import RiskLimitsConfig
from src.validation.protocol import ManifestDriftError, StrategyVersionManifest, verify_manifest_integrity
from src.validation.session import DailySnapshot, RuleViolationRecord

AlertSeverity = Literal["info", "warning", "critical"]


class AlertCode(str, Enum):
    DRAWDOWN_WARNING = "drawdown_warning"
    DRAWDOWN_CRITICAL = "drawdown_critical"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    WEEKLY_LOSS_THRESHOLD = "weekly_loss_threshold"
    RULE_VIOLATION = "rule_violation"
    MANIFEST_DRIFT = "manifest_drift"
    INSUFFICIENT_SAMPLE_AT_90 = "insufficient_sample_at_90"


@dataclass(frozen=True)
class Alert:
    code: AlertCode
    severity: AlertSeverity
    message: str
    occurred_at: datetime


def _drawdown_alerts(snapshot: DailySnapshot, limits: RiskLimitsConfig, now: datetime) -> list[Alert]:
    alerts: list[Alert] = []
    if snapshot.drawdown_pct >= limits.drawdown_halt_pct:
        alerts.append(
            Alert(
                code=AlertCode.DRAWDOWN_CRITICAL, severity="critical",
                message=f"drawdown {snapshot.drawdown_pct:.2%} at/above the Risk Engine's halt threshold "
                f"({limits.drawdown_halt_pct:.2%})",
                occurred_at=now,
            )
        )
    elif snapshot.drawdown_pct >= limits.drawdown_warning_pct:
        alerts.append(
            Alert(
                code=AlertCode.DRAWDOWN_WARNING, severity="warning",
                message=f"drawdown {snapshot.drawdown_pct:.2%} at/above the warning threshold "
                f"({limits.drawdown_warning_pct:.2%})",
                occurred_at=now,
            )
        )
    return alerts


def _consecutive_loss_alert(recent_trade_pnls: list[float], *, threshold: int, now: datetime) -> Alert | None:
    """Checks only the trailing `threshold` trades in chronological
    order — a streak anywhere earlier in the run that has since broken
    does not re-fire."""
    if len(recent_trade_pnls) < threshold:
        return None
    trailing = recent_trade_pnls[-threshold:]
    if all(p < 0 for p in trailing):
        return Alert(
            code=AlertCode.CONSECUTIVE_LOSSES, severity="warning",
            message=f"{threshold} consecutive losing trades",
            occurred_at=now,
        )
    return None


def _weekly_loss_alert(weekly_pnl: float, starting_nav: float, *, threshold_pct: float, now: datetime) -> Alert | None:
    if starting_nav <= 0:
        raise ValueError("starting_nav must be positive")
    loss_pct = -weekly_pnl / starting_nav
    if loss_pct >= threshold_pct:
        return Alert(
            code=AlertCode.WEEKLY_LOSS_THRESHOLD, severity="warning",
            message=f"weekly loss {loss_pct:.2%} of starting NAV at/above the {threshold_pct:.2%} alert threshold",
            occurred_at=now,
        )
    return None


def _violation_alerts(violations: list[RuleViolationRecord], now: datetime) -> list[Alert]:
    return [
        Alert(code=AlertCode.RULE_VIOLATION, severity="critical", message=f"{v.rule_name}: {v.description}", occurred_at=now)
        for v in violations
    ]


def _manifest_drift_alert(manifest: StrategyVersionManifest | None, now: datetime) -> Alert | None:
    if manifest is None:
        return None
    try:
        verify_manifest_integrity(manifest)
    except ManifestDriftError as exc:
        return Alert(code=AlertCode.MANIFEST_DRIFT, severity="critical", message=str(exc), occurred_at=now)
    return None


def generate_alerts(
    *,
    snapshot: DailySnapshot,
    limits: RiskLimitsConfig,
    recent_trade_pnls: list[float],
    consecutive_loss_alert_count: int,
    weekly_pnl: float | None,
    starting_nav: float,
    weekly_loss_alert_pct: float,
    new_violations: list[RuleViolationRecord],
    manifest: StrategyVersionManifest | None,
    now: datetime,
) -> list[Alert]:
    """Evaluates every alert condition against one day's state and
    returns whatever fired — zero or more. Callers are expected to call
    this once per day (or once per new event); nothing here is stateful
    across calls, so de-duplicating repeat-fires across days is the
    caller's responsibility, same as `src.risk.engine` leaves "what to
    do with a REJECT" to its own caller."""
    alerts: list[Alert] = []
    alerts.extend(_drawdown_alerts(snapshot, limits, now))

    consecutive = _consecutive_loss_alert(recent_trade_pnls, threshold=consecutive_loss_alert_count, now=now)
    if consecutive is not None:
        alerts.append(consecutive)

    if weekly_pnl is not None:
        weekly = _weekly_loss_alert(weekly_pnl, starting_nav, threshold_pct=weekly_loss_alert_pct, now=now)
        if weekly is not None:
            alerts.append(weekly)

    alerts.extend(_violation_alerts(new_violations, now))

    drift = _manifest_drift_alert(manifest, now)
    if drift is not None:
        alerts.append(drift)

    return alerts


def insufficient_sample_alert(now: datetime) -> Alert:
    """Raised by the caller (not auto-detected here — `src.validation
    .gates.evaluate_90_day_gate` already refuses to run before day 90)
    at day 90 if `sample_size_status` came back `INSUFFICIENT_SAMPLE`."""
    return Alert(
        code=AlertCode.INSUFFICIENT_SAMPLE_AT_90, severity="warning",
        message="validation run reached day 90 with fewer than the minimum required completed trades; "
        "the 90-day gate classified EXTEND_VALIDATION",
        occurred_at=now,
    )
