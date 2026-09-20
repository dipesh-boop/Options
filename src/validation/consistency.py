"""Weekly/monthly consistency, correlation analysis, and 100%
hard-rule-compliance tracking for a validation run.

**Weekly reviews never alter rules from a single week's data** —
enforced here the same way `src.workflows.weekly_review` enforces "this
module cannot modify production strategy": nothing in this module
imports `src.risk.limits`'s *writer* path (there isn't one — limits are
YAML, edited by a human, never by code) or `src.research.promotion`.
`build_weekly_consistency` only ever produces a report; there is no
function anywhere in this package that could act on one.

**Rule compliance is an audit, not an enforcement mechanism.** The Risk
Engine's veto (`src.risk.engine.evaluate_trade_proposal`) is already
unconditional and absolute — nothing here makes it more so.
`check_rule_compliance` instead answers a narrower, after-the-fact
question: did every trade *recorded in this validation run* actually
carry a Risk-Engine APPROVE/RESIZE decision, or does the record itself
show a gap (a trade with no accompanying risk decision, a REJECT/HALT
that somehow still closed) that the run's own bookkeeping — not the Risk
Engine — is responsible for catching.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np

from src.backtest.simulator import TradeRecord
from src.quant.correlations import CorrelatedPair, flag_highly_correlated_pairs
from src.risk.reason_codes import RiskDecision
from src.validation.session import RuleViolationRecord

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)


@dataclass(frozen=True)
class WeeklyConsistencyRecord:
    week_start: date
    week_end: date
    trade_count: int
    realized_pnl: float
    win_rate: float


def build_weekly_consistency(trades: list[TradeRecord], *, period_start: date) -> list[WeeklyConsistencyRecord]:
    """Buckets closed trades into consecutive 7-day windows anchored on
    `period_start` (the validation period's own start date, not the
    calendar week) — so "week 1" always means "the first 7 days of this
    validation run," consistent regardless of what day of the week the
    run actually started."""
    buckets: dict[int, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        week_index = (t.closed_at - period_start).days // 7
        buckets[week_index].append(t)

    records: list[WeeklyConsistencyRecord] = []
    for week_index in sorted(buckets):
        week_trades = buckets[week_index]
        week_start = period_start + timedelta(days=week_index * 7)
        week_end = week_start + timedelta(days=6)
        pnls = [t.realistic_pnl for t in week_trades]
        winners = sum(1 for p in pnls if p > 0)
        records.append(
            WeeklyConsistencyRecord(
                week_start=week_start, week_end=week_end, trade_count=len(week_trades),
                realized_pnl=sum(pnls), win_rate=winners / len(pnls) if pnls else 0.0,
            )
        )
    return records


@dataclass(frozen=True)
class ConsistencySummary:
    weeks_with_trades: int
    profitable_week_count: int
    profitable_week_pct: float
    average_weekly_pnl: float
    weekly_pnl_stdev: float
    worst_week_pnl: float
    best_week_pnl: float
    longest_losing_week_streak: int


def summarize_consistency(records: list[WeeklyConsistencyRecord]) -> ConsistencySummary:
    active = [r for r in records if r.trade_count > 0]
    if not active:
        return ConsistencySummary(
            weeks_with_trades=0, profitable_week_count=0, profitable_week_pct=0.0,
            average_weekly_pnl=0.0, weekly_pnl_stdev=0.0, worst_week_pnl=0.0, best_week_pnl=0.0,
            longest_losing_week_streak=0,
        )
    pnls = [r.realized_pnl for r in active]
    profitable = sum(1 for p in pnls if p > 0)

    streak = 0
    longest_streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0

    return ConsistencySummary(
        weeks_with_trades=len(active),
        profitable_week_count=profitable,
        profitable_week_pct=profitable / len(active),
        average_weekly_pnl=float(np.mean(pnls)),
        weekly_pnl_stdev=float(np.std(pnls, ddof=1)) if len(pnls) >= 2 else 0.0,
        worst_week_pnl=min(pnls),
        best_week_pnl=max(pnls),
        longest_losing_week_streak=longest_streak,
    )


def correlation_summary(price_history: dict[str, list[float]], *, threshold: float) -> list[CorrelatedPair]:
    """Thin wrapper over `src.quant.correlations.flag_highly_correlated_pairs`
    — the same underlying computation `src.risk.correlation.check_correlation`
    already uses live, applied here for reporting rather than gating."""
    arrays = {ticker: np.array(prices, dtype=float) for ticker, prices in price_history.items()}
    return flag_highly_correlated_pairs(arrays, threshold=threshold)


@dataclass(frozen=True)
class RuleComplianceSummary:
    total_trades_checked: int
    violations_found: int
    compliance_pct: float
    violations: tuple[RuleViolationRecord, ...]


def check_rule_compliance(
    trade_risk_decisions: list[tuple[TradeRecord, RiskDecision | None]],
    *,
    logged_violations: list[RuleViolationRecord],
) -> RuleComplianceSummary:
    """Flags any trade whose accompanying risk decision is missing or is
    not APPROVE/RESIZE as a compliance gap, in addition to whatever was
    already explicitly logged via `RuleViolationRecord` elsewhere
    (e.g. a manifest-drift or kill-switch-bypass finding) —
    `logged_violations` and trade-decision gaps are combined into one
    compliance figure since both represent the same underlying question:
    did every trade in this run actually clear the Risk Engine."""
    derived_violations: list[RuleViolationRecord] = []
    for trade, decision in trade_risk_decisions:
        if decision not in _ACCEPTABLE_RISK_DECISIONS:
            derived_violations.append(
                RuleViolationRecord(
                    violation_id=f"compliance-gap-{trade.position_id}",
                    occurred_at=datetime.combine(trade.closed_at, datetime.min.time(), tzinfo=timezone.utc),
                    rule_name="risk_engine_approval_required",
                    description=(
                        f"trade {trade.position_id} ({trade.ticker}) has no recorded APPROVE/RESIZE "
                        f"Risk Engine decision (found {decision!r})"
                    ),
                    related_id=trade.position_id,
                )
            )

    total = len(trade_risk_decisions)
    all_violations = tuple(logged_violations) + tuple(derived_violations)
    violation_count = len(all_violations)
    compliance_pct = 1.0 if total == 0 else max(0.0, 1.0 - (violation_count / total))
    return RuleComplianceSummary(
        total_trades_checked=total, violations_found=violation_count,
        compliance_pct=compliance_pct, violations=all_violations,
    )
