"""Assembles a `ValidationReportBundle` from a `ValidationStore` --
the single place every export format (CSV/JSON/XLSX/PDF) reads from,
so no two formats can ever report a different number for the same
figure. Every computed field here is a direct read or a simple,
transparent aggregation over already-canonical stored records -- see
this package's own `__init__.py` docstring for the full reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.backtest.simulator import TradeRecord
from src.validation.counterfactual import StrategyAlternativeRecord
from src.validation.records import CohortRecord, OpportunityRecord
from src.validation.session import DailySnapshot, ReconciliationFailureRecord, RuleViolationRecord, ValidationStore


@dataclass(frozen=True)
class StrategyPerformanceRow:
    """One strategy's aggregate performance over every trade this
    cohort has recorded for it -- `total_pnl`/`win_rate`/`expectancy`
    are plain sums/means/ratios over each trade's own canonical
    `realistic_pnl` (never a re-derived P&L)."""

    strategy: str
    trade_count: int
    win_count: int
    win_rate: float
    total_realistic_pnl: float
    total_theoretical_pnl: float
    total_commission_paid: float
    average_realistic_pnl: float
    expectancy: float  # == average_realistic_pnl, named for reporting-audience clarity
    largest_win: float
    largest_loss: float


@dataclass(frozen=True)
class RiskMetricsSummary:
    """Read directly from the cohort's own `DailySnapshot` history and
    `RuleViolationRecord`s -- no independent drawdown/risk
    recomputation. `peak_nav`/`trough_nav`/`max_drawdown_pct` are
    exact reductions (max/min/max-relative-decline) over the stored
    NAV series, not an estimate."""

    snapshot_count: int
    starting_nav: float | None
    latest_nav: float | None
    peak_nav: float | None
    trough_nav: float | None
    max_drawdown_pct: float | None
    latest_drawdown_pct: float | None
    violation_count: int
    unresolved_reconciliation_failure_count: int


@dataclass(frozen=True)
class ExecutionQualityRow:
    """Slippage/spread figures read directly from each `TradeRecord`'s
    own `realistic_entry_credit`/`theoretical_entry_credit` gap (the
    same "realistic vs. theoretical" distinction the pipeline itself
    already tracks per trade) -- a mean over already-computed
    per-trade numbers, not a new pricing model."""

    strategy: str
    trade_count: int
    average_entry_spread_pct: float
    average_realistic_vs_theoretical_entry_gap: float
    total_commission_paid: float


@dataclass(frozen=True)
class DecisionQualityRow:
    """One opportunity's decision-quality facts, read directly from
    its stored `OpportunityRecord` -- never re-judged."""

    opportunity_id: str
    ticker: str
    pipeline_status: str | None
    risk_decision: str | None
    cash_no_trade: bool
    alternative_count: int


@dataclass(frozen=True)
class SelectionRecordRow:
    """One strategy alternative considered for one opportunity, exactly
    as recorded at decision time (never reconstructed with hindsight --
    see `src.validation.counterfactual`'s own module docstring)."""

    opportunity_id: str
    strategy_kind: str
    was_selected: bool
    selection_or_rejection_reason: str
    expected_value: float
    maximum_loss: float
    probability_of_profit: float


@dataclass(frozen=True)
class ValidationReportBundle:
    """Everything every export format reads from -- built once per
    export run, from one `ValidationStore` snapshot, so CSV/JSON/XLSX/
    PDF can never disagree with each other."""

    export_schema_version: str
    generated_at: datetime
    cohort: CohortRecord | None
    trades: tuple[TradeRecord, ...]
    snapshots: tuple[DailySnapshot, ...]
    opportunities: tuple[OpportunityRecord, ...]
    violations: tuple[RuleViolationRecord, ...]
    reconciliation_failures: tuple[ReconciliationFailureRecord, ...]
    strategy_performance: tuple[StrategyPerformanceRow, ...]
    risk_metrics: RiskMetricsSummary
    execution_quality: tuple[ExecutionQualityRow, ...]
    decision_quality: tuple[DecisionQualityRow, ...]
    selection_records: tuple[SelectionRecordRow, ...]
    benchmark_comparison_note: str = (
        "N/A -- this platform has no live external benchmark price-feed connection yet "
        "(confirmed absent; see ACCEPTANCE_TEST_REPORT.md and STEP_22_FREEZE_REPORT.md). "
        "src.validation.benchmarks.compare_against_benchmarks is a real, already-tested "
        "capability a future live-data orchestrator can wire in once a benchmark feed exists."
    )
    statistical_scorecard_note: str = (
        "N/A -- a full bootstrap/Monte-Carlo-derived Sharpe/Sortino/VaR/CVaR scorecard "
        "(src.validation.scorecard.build_scorecard) requires a completed equity curve with "
        "meaningful sample size; this cohort does not have one yet. The scorecard machinery "
        "itself is real and already tested -- see tests/unit/validation/test_scorecard.py."
    )


EXPORT_SCHEMA_VERSION = "1.0.0"


def _strategy_performance(trades: list[TradeRecord]) -> tuple[StrategyPerformanceRow, ...]:
    by_strategy: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_strategy.setdefault(t.strategy.value, []).append(t)

    rows = []
    for strategy, group in sorted(by_strategy.items()):
        pnls = [t.realistic_pnl for t in group]
        wins = [p for p in pnls if p > 0]
        rows.append(StrategyPerformanceRow(
            strategy=strategy,
            trade_count=len(group),
            win_count=len(wins),
            win_rate=len(wins) / len(group) if group else 0.0,
            total_realistic_pnl=sum(pnls),
            total_theoretical_pnl=sum(t.theoretical_pnl for t in group),
            total_commission_paid=sum(t.commission_paid for t in group),
            average_realistic_pnl=sum(pnls) / len(group) if group else 0.0,
            expectancy=sum(pnls) / len(group) if group else 0.0,
            largest_win=max(pnls) if pnls else 0.0,
            largest_loss=min(pnls) if pnls else 0.0,
        ))
    return tuple(rows)


def _risk_metrics(snapshots: list[DailySnapshot], violations: list[RuleViolationRecord], reconciliation_failures: list[ReconciliationFailureRecord]) -> RiskMetricsSummary:
    if not snapshots:
        return RiskMetricsSummary(
            snapshot_count=0, starting_nav=None, latest_nav=None, peak_nav=None, trough_nav=None,
            max_drawdown_pct=None, latest_drawdown_pct=None, violation_count=len(violations),
            unresolved_reconciliation_failure_count=sum(1 for f in reconciliation_failures if not f.resolved),
        )
    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    navs = [s.nav for s in ordered]
    peak_so_far = navs[0]
    max_dd = 0.0
    for nav in navs:
        peak_so_far = max(peak_so_far, nav)
        dd = (peak_so_far - nav) / peak_so_far if peak_so_far > 0 else 0.0
        max_dd = max(max_dd, dd)
    peak_nav = max(navs)
    latest_nav = navs[-1]
    return RiskMetricsSummary(
        snapshot_count=len(ordered),
        starting_nav=navs[0],
        latest_nav=latest_nav,
        peak_nav=peak_nav,
        trough_nav=min(navs),
        max_drawdown_pct=max_dd,
        latest_drawdown_pct=(peak_nav - latest_nav) / peak_nav if peak_nav > 0 else 0.0,
        violation_count=len(violations),
        unresolved_reconciliation_failure_count=sum(1 for f in reconciliation_failures if not f.resolved),
    )


def _execution_quality(trades: list[TradeRecord]) -> tuple[ExecutionQualityRow, ...]:
    by_strategy: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_strategy.setdefault(t.strategy.value, []).append(t)

    rows = []
    for strategy, group in sorted(by_strategy.items()):
        gaps = [abs(t.realistic_entry_credit - t.theoretical_entry_credit) for t in group]
        spreads = [t.entry_spread_pct for t in group]
        rows.append(ExecutionQualityRow(
            strategy=strategy,
            trade_count=len(group),
            average_entry_spread_pct=sum(spreads) / len(group) if group else 0.0,
            average_realistic_vs_theoretical_entry_gap=sum(gaps) / len(group) if group else 0.0,
            total_commission_paid=sum(t.commission_paid for t in group),
        ))
    return tuple(rows)


def _decision_quality(opportunities: list[OpportunityRecord]) -> tuple[DecisionQualityRow, ...]:
    return tuple(
        DecisionQualityRow(
            opportunity_id=o.opportunity_id, ticker=o.ticker, pipeline_status=o.pipeline_status,
            risk_decision=o.risk_decision.decision.value if o.risk_decision else None,
            cash_no_trade=o.cash_no_trade, alternative_count=len(o.alternatives),
        )
        for o in sorted(opportunities, key=lambda o: o.created_at)
    )


def _selection_records(opportunities: list[OpportunityRecord]) -> tuple[SelectionRecordRow, ...]:
    rows = []
    for o in sorted(opportunities, key=lambda o: o.created_at):
        for alt in o.alternatives:
            ev = alt.evaluation
            rows.append(SelectionRecordRow(
                opportunity_id=o.opportunity_id, strategy_kind=ev.strategy_kind.value,
                was_selected=alt.was_selected, selection_or_rejection_reason=alt.selection_or_rejection_reason,
                expected_value=ev.expected_value, maximum_loss=ev.maximum_loss,
                probability_of_profit=ev.probability_metrics.probability_of_profit,
            ))
    return tuple(rows)


def build_report_bundle(store: ValidationStore, *, cohort_id: str | None = None, generated_at: datetime) -> ValidationReportBundle:
    """Reads everything a real export needs from `store` exactly once,
    in one pass -- the ONLY place this happens, so every export format
    built from the returned bundle is guaranteed internally consistent
    with every other."""
    cohort = store.get_cohort(cohort_id) if cohort_id else (store.cohorts()[0] if store.cohorts() else None)
    resolved_cohort_id = cohort.cohort_id if cohort is not None else cohort_id

    # NOTE: `trades()`/`violations()`/`rejected_outcomes()` are not
    # cohort-scoped in storage (unlike `snapshots()`/`opportunities()`)
    # -- Step 19's original design never anticipated multiple cohorts
    # sharing one store, and V1.0 never runs more than one cohort at a
    # time in practice. `snapshots()`/`opportunities()` ARE properly
    # cohort-scoped since Step 22 introduced them with that in mind.
    trades = list(store.trades())
    snapshots = list(store.snapshots(cohort_id=resolved_cohort_id) if resolved_cohort_id else store.snapshots())
    opportunities = list(store.opportunities(cohort_id=resolved_cohort_id) if resolved_cohort_id else store.opportunities())
    violations = list(store.violations())
    reconciliation_failures = list(store.reconciliation_failures())

    return ValidationReportBundle(
        export_schema_version=EXPORT_SCHEMA_VERSION,
        generated_at=generated_at,
        cohort=cohort,
        trades=tuple(trades),
        snapshots=tuple(snapshots),
        opportunities=tuple(opportunities),
        violations=tuple(violations),
        reconciliation_failures=tuple(reconciliation_failures),
        strategy_performance=_strategy_performance(trades),
        risk_metrics=_risk_metrics(snapshots, violations, reconciliation_failures),
        execution_quality=_execution_quality(trades),
        decision_quality=_decision_quality(opportunities),
        selection_records=_selection_records(opportunities),
    )
