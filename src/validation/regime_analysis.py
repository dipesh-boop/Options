"""Market-regime robustness analysis across a validation run.

**Interpretation call, flagged rather than silently decided**: Step 19
asks for analysis across "5 regimes" without naming them.
`src.llm.schemas.MarketRegimeLabel` (the Multi-Agent Layer's own regime
literal) only has 4 values (`low_vol`/`normal`/`elevated_vol`/`crisis`) —
reused as-is elsewhere in this codebase, but insufficient here, since it
collapses direction (bull/bear) entirely into volatility level. This
module defines its own 5-value `ValidationRegime`
(`bull_trending`/`bear_trending`/`range_bound_low_vol`/
`range_bound_high_vol`/`crisis_tail_event`) because direction matters at
least as much as volatility level for this platform's short-premium
strategies (a trending bear market is a materially different regime for
a cash-secured put than a range-bound high-vol one, even at the same VIX
level) — a second, deliberately distinct taxonomy from
`MarketRegimeLabel`, not a redefinition of it.

Regime bucketing itself reuses `src.research.performance_breakdown
.breakdown_by` (its `market_regime` dimension already takes a plain
string, so this module's 5 labels slot in directly) rather than a new
bucketing implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.research.performance_breakdown import (
    PerformanceBreakdownReport,
    ResearchTradeObservation,
    TradeContext,
    breakdown_by,
)

ValidationRegime = Literal[
    "bull_trending", "bear_trending", "range_bound_low_vol", "range_bound_high_vol", "crisis_tail_event",
]

ALL_VALIDATION_REGIMES: tuple[ValidationRegime, ...] = (
    "bull_trending", "bear_trending", "range_bound_low_vol", "range_bound_high_vol", "crisis_tail_event",
)

_TREND_THRESHOLD = 0.02  # +/- 2% trailing SPY return distinguishes trending from range-bound
_HIGH_VOL_VIX = 20.0
_CRISIS_VIX = 30.0


def classify_regime(*, spy_trailing_return: float, vix_level: float) -> ValidationRegime:
    """A deliberately simple, documented heuristic — the same
    "provisional, not the eventual real thing" status
    `src.workflows.candidate_generation`'s screener already carries for
    its own closest-to-target-delta selection. `vix_level` and
    `spy_trailing_return` (e.g. a trailing 20-trading-day SPY return) are
    caller-supplied, exactly like every other market-context input this
    codebase takes (`MarketRegimeAssessment`, `MarketSnapshotContext`) —
    this module does not fetch or invent either."""
    if vix_level < 0:
        raise ValueError("vix_level cannot be negative")
    if vix_level >= _CRISIS_VIX:
        return "crisis_tail_event"
    if spy_trailing_return >= _TREND_THRESHOLD:
        return "bull_trending"
    if spy_trailing_return <= -_TREND_THRESHOLD:
        return "bear_trending"
    return "range_bound_high_vol" if vix_level >= _HIGH_VOL_VIX else "range_bound_low_vol"


def tag_observation_with_regime(context: TradeContext, regime: ValidationRegime) -> TradeContext:
    """Returns a new `TradeContext` with `market_regime` overridden to
    `regime` — `TradeContext` is frozen, so this never mutates the
    caller's object."""
    return TradeContext(
        entry_delta=context.entry_delta, entry_dte=context.entry_dte, iv_percentile=context.iv_percentile,
        market_regime=regime, sector=context.sector, profit_target_pct=context.profit_target_pct,
        management_dte=context.management_dte, entry_time_of_day=context.entry_time_of_day,
    )


def regime_breakdown(observations: list[ResearchTradeObservation]) -> PerformanceBreakdownReport:
    """`src.research.performance_breakdown.breakdown_by`, unmodified,
    against the `market_regime` dimension."""
    return breakdown_by(observations, "market_regime")


@dataclass(frozen=True)
class RegimeCoverageSummary:
    """How many of the 5 regimes this validation run actually observed,
    and how concentrated trades were within them — a 90-day window
    realistically sees only 1-2 regimes, and a report claiming
    "regime-robust" without saying so would be misleading. Mirrors the
    honesty discipline `src.workflows.rejected_trade_review
    .RejectedTradeStatistics.meaningful_sample` already applies to
    small samples, applied here to regime coverage instead."""

    regimes_observed: tuple[ValidationRegime, ...]
    regimes_not_observed: tuple[ValidationRegime, ...]
    trade_count_by_regime: dict[str, int]
    single_regime_dominant: bool  # True if >80% of trades fell in one regime


def regime_coverage_summary(observations: list[ResearchTradeObservation]) -> RegimeCoverageSummary:
    counts: dict[str, int] = {}
    for obs in observations:
        counts[obs.context.market_regime] = counts.get(obs.context.market_regime, 0) + 1
    observed = tuple(r for r in ALL_VALIDATION_REGIMES if r in counts)
    not_observed = tuple(r for r in ALL_VALIDATION_REGIMES if r not in counts)
    total = sum(counts.values())
    dominant = total > 0 and max(counts.values()) / total > 0.80
    return RegimeCoverageSummary(
        regimes_observed=observed, regimes_not_observed=not_observed,
        trade_count_by_regime=counts, single_regime_dominant=dominant,
    )
