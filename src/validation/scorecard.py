"""The validation report's scorecard: multiple named categories, never
collapsed into one composite score.

This is a deliberate structural choice, not a presentation preference —
`ValidationScorecard` has no field anywhere that could hold a single
"overall score" (`tests/unit/validation/test_scorecard.py` asserts this
directly by inspecting the dataclass's own field names), because a
single number would let a strong RETURN category silently paper over a
failing COMPLIANCE or SAMPLE_ADEQUACY category — exactly the kind of
collapse `src.workflows.decision_quality`'s four-quadrant classifier
already refuses to do for a single trade, applied here at the
whole-report level instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.validation.consistency import ConsistencySummary, RuleComplianceSummary
from src.validation.decision_quality import ValidationDecisionQualityReport
from src.validation.execution_quality import OptionsExecutionMetrics
from src.validation.protocol import SampleSizeStatus, ValidationPeriod
from src.validation.regime_analysis import RegimeCoverageSummary
from src.workflows.execution_quality import SlippageSummary
from src.workflows.rejected_trade_review import RejectedTradeStatistics

_CATEGORY_NAMES = ("RETURN", "RISK", "TRADE_QUALITY", "EXECUTION", "CONSISTENCY", "COMPLIANCE", "SAMPLE_ADEQUACY")


@dataclass(frozen=True)
class ScorecardCategory:
    name: str
    metrics: dict[str, object]
    qualitative_note: str


@dataclass(frozen=True)
class ValidationScorecard:
    """Field-by-field, not `overall_score: float` — see module docstring.
    `categories` is a fixed-order tuple of exactly the names in
    `_CATEGORY_NAMES`, one per category, never merged."""

    generated_at: datetime
    period: ValidationPeriod
    categories: tuple[ScorecardCategory, ...]

    def category(self, name: str) -> ScorecardCategory:
        for c in self.categories:
            if c.name == name:
                return c
        raise KeyError(f"no scorecard category named {name!r}")


@dataclass(frozen=True)
class ReturnInputs:
    since_inception_return: float
    annualized_return_projected: float
    excess_vs_spy: float
    excess_vs_risk_free: float
    excess_vs_cash: float


@dataclass(frozen=True)
class RiskInputs:
    sharpe: float
    sortino: float
    max_drawdown: float
    current_drawdown_pct: float
    var_95: float
    cvar_95: float


def build_scorecard(
    *,
    generated_at: datetime,
    period: ValidationPeriod,
    sample_status: SampleSizeStatus,
    completed_trades: int,
    return_inputs: ReturnInputs,
    risk_inputs: RiskInputs,
    decision_quality: ValidationDecisionQualityReport,
    rejected_trades: RejectedTradeStatistics,
    slippage: SlippageSummary,
    options_execution: OptionsExecutionMetrics,
    consistency: ConsistencySummary,
    regime_coverage: RegimeCoverageSummary,
    compliance: RuleComplianceSummary,
) -> ValidationScorecard:
    categories = (
        ScorecardCategory(
            name="RETURN",
            metrics={
                "since_inception_return": return_inputs.since_inception_return,
                "annualized_return_projected": return_inputs.annualized_return_projected,
                "excess_vs_spy": return_inputs.excess_vs_spy,
                "excess_vs_risk_free": return_inputs.excess_vs_risk_free,
                "excess_vs_cash": return_inputs.excess_vs_cash,
            },
            qualitative_note="Excess-vs-benchmark figures are OBSERVED for the period elapsed; "
            "annualized_return_projected is a PROJECTED extrapolation, never a measurement.",
        ),
        ScorecardCategory(
            name="RISK",
            metrics={
                "sharpe": risk_inputs.sharpe,
                "sortino": risk_inputs.sortino,
                "max_drawdown": risk_inputs.max_drawdown,
                "current_drawdown_pct": risk_inputs.current_drawdown_pct,
                "var_95": risk_inputs.var_95,
                "cvar_95": risk_inputs.cvar_95,
            },
            qualitative_note="All risk figures are OBSERVED from the run's own equity curve.",
        ),
        ScorecardCategory(
            name="TRADE_QUALITY",
            metrics={
                "decision_quality_combined": dict(decision_quality.combined.counts),
                "decision_quality_by_strategy": {
                    k: dict(v.counts) for k, v in decision_quality.by_strategy.items()
                },
                "rejected_trade_hit_rate": rejected_trades.hit_rate,
                "rejected_trade_meaningful_sample": rejected_trades.meaningful_sample,
            },
            qualitative_note="Decision quality is classified from ex-ante facts only, never realized P&L — "
            "see src.workflows.decision_quality. Rejected-trade conclusions require "
            f"meaningful_sample=True to be trusted (currently {rejected_trades.meaningful_sample}).",
        ),
        ScorecardCategory(
            name="EXECUTION",
            metrics={
                "average_slippage": slippage.average_slippage,
                "median_slippage": slippage.median_slippage,
                "fill_rate": options_execution.fill_rate,
                "assignment_rate": options_execution.assignment_rate,
                "early_close_rate": options_execution.early_close_rate,
            },
            qualitative_note="fill_rate is None unless the caller supplied an attempted-trade count "
            "(never fabricated).",
        ),
        ScorecardCategory(
            name="CONSISTENCY",
            metrics={
                "profitable_week_pct": consistency.profitable_week_pct,
                "weekly_pnl_stdev": consistency.weekly_pnl_stdev,
                "longest_losing_week_streak": consistency.longest_losing_week_streak,
                "regimes_observed": list(regime_coverage.regimes_observed),
                "regimes_not_observed": list(regime_coverage.regimes_not_observed),
                "single_regime_dominant": regime_coverage.single_regime_dominant,
            },
            qualitative_note="A 90-day window may realistically cover only 1-2 of the 5 defined regimes — "
            "'regime-robust' cannot be claimed for a regime never observed.",
        ),
        ScorecardCategory(
            name="COMPLIANCE",
            metrics={
                "total_trades_checked": compliance.total_trades_checked,
                "violations_found": compliance.violations_found,
                "compliance_pct": compliance.compliance_pct,
            },
            qualitative_note="Any violations_found > 0 is treated as blocking by src.validation.gates, "
            "regardless of every other category's value.",
        ),
        ScorecardCategory(
            name="SAMPLE_ADEQUACY",
            metrics={
                "completed_trades": completed_trades,
                "sample_status": sample_status.value,
            },
            qualitative_note="INSUFFICIENT_SAMPLE at day 90 means the 90-day gate cannot classify PASS "
            "no matter how strong the other categories look.",
        ),
    )
    return ValidationScorecard(generated_at=generated_at, period=period, categories=categories)
