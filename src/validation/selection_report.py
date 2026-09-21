"""The Strategy Selection Report: a dedicated report answering whether
the Strategy Selector itself is adding value, distinct from
`src.validation.strategy_attribution`'s per-strategy performance
report (which never asks whether picking this strategy over its
alternatives was the right call).

Composed from two already-built, independently-tested sources rather
than a new computation engine:

- `src.validation.counterfactual` — `CounterfactualOutcome` records
  (one per strategy actually considered for an opportunity, selected or
  not), `summarize_selection_effectiveness`/`summarize_by_regime`
  (Selection Regret), and the new `dynamic_vs_fixed_strategy_comparison`.
- `src.validation.strategy_attribution` — `StrategyPerformanceSummary`
  (completed-trade stats) and `answer_attribution_questions` (four of
  this report's six named questions overlap exactly with Step 14B's own
  six attribution questions — reused here, never re-answered by a
  second implementation).

This module imports both `counterfactual` and `strategy_attribution`;
neither of those imports this one or each other in that direction
(`strategy_attribution` imports `counterfactual`, not the reverse), so
this is the natural place for their combination to live without a
cycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.validation.counterfactual import (
    CounterfactualOutcome,
    DynamicVsFixedComparison,
    MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS,
    SelectionEffectivenessSummary,
    dynamic_vs_fixed_strategy_comparison,
    summarize_by_regime,
    summarize_selection_effectiveness,
)
from src.validation.strategy_attribution import (
    StrategyAttributionReport,
    StrategyPerformanceSummary,
    answer_attribution_questions,
)


@dataclass(frozen=True)
class SelectionFrequency:
    strategy: str
    times_selected: int
    selection_share: float  # times_selected / total selections across every strategy, 0.0 when nothing was ever selected


def _selection_frequency(outcomes: list[CounterfactualOutcome]) -> dict[str, SelectionFrequency]:
    selected = [o for o in outcomes if o.was_selected]
    total = len(selected)
    counts: dict[str, int] = {}
    for o in selected:
        counts[o.strategy_kind.value] = counts.get(o.strategy_kind.value, 0) + 1
    return {
        strategy: SelectionFrequency(strategy=strategy, times_selected=n, selection_share=(n / total) if total > 0 else 0.0)
        for strategy, n in counts.items()
    }


def _risk_adjusted_value_by_strategy(summaries: dict[str, StrategyPerformanceSummary]) -> dict[str, float]:
    """Expectancy per dollar of capital actually deployed -- the
    realized-trade-data analogue of `src.strategies.ranking
    .risk_adjusted_score` (which uses ex-ante expected value and
    maximum loss; this uses ex-post average P&L and average capital
    required, since that is what completed-trade data actually has)."""
    return {
        strategy: (summary.expectancy / summary.avg_capital_deployed)
        for strategy, summary in summaries.items()
        if summary.avg_capital_deployed > 0
    }


@dataclass(frozen=True)
class StrategySelectionReport:
    selection_frequency: dict[str, SelectionFrequency]  # "which strategies are being selected most often?"
    risk_adjusted_value: dict[str, float]  # "which strategies generate the most risk-adjusted value?"
    reduces_portfolio_risk: tuple[str, ...]  # "which strategies primarily reduce portfolio risk?"
    consumes_capital_without_benefit: tuple[str, ...]  # "which strategies consume capital without sufficient benefit?"
    regime_specific: tuple[str, ...]  # "which strategies work only in specific regimes?"
    excessive_execution_costs: tuple[str, ...]  # "which strategies create excessive execution costs?"
    dynamic_vs_fixed: DynamicVsFixedComparison  # "is dynamic selection outperforming a simpler fixed-strategy approach?"
    overall_effectiveness: SelectionEffectivenessSummary
    effectiveness_by_market_regime: dict[str, SelectionEffectivenessSummary]
    effectiveness_by_volatility_regime: dict[str, SelectionEffectivenessSummary]
    attribution: StrategyAttributionReport  # the full underlying answer set, never collapsed away


def build_strategy_selection_report(
    outcomes: list[CounterfactualOutcome],
    performance_summaries: dict[str, StrategyPerformanceSummary],
    *,
    min_sample_size: int = MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS,
) -> StrategySelectionReport:
    attribution = answer_attribution_questions(performance_summaries)
    return StrategySelectionReport(
        selection_frequency=_selection_frequency(outcomes),
        risk_adjusted_value=_risk_adjusted_value_by_strategy(performance_summaries),
        reduces_portfolio_risk=attribution.reduced_losses,
        consumes_capital_without_benefit=attribution.consumed_capital_without_value,
        regime_specific=attribution.regime_specific,
        excessive_execution_costs=attribution.generated_excessive_trading_costs,
        dynamic_vs_fixed=dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=min_sample_size),
        overall_effectiveness=summarize_selection_effectiveness(outcomes, min_sample_size=min_sample_size),
        effectiveness_by_market_regime=summarize_by_regime(outcomes, key="market_regime", min_sample_size=min_sample_size),
        effectiveness_by_volatility_regime=summarize_by_regime(outcomes, key="volatility_regime", min_sample_size=min_sample_size),
        attribution=attribution,
    )


def _fmt_meaningful(s: SelectionEffectivenessSummary) -> str:
    if not s.meaningful_sample:
        return f"INSUFFICIENT SAMPLE ({s.warning})"
    return (
        f"selected expectancy ${s.selected_expectancy:,.2f}, alternative expectancy ${s.average_alternative_expectancy:,.2f}, "
        f"NO_TRADE ${s.no_trade_expectancy:,.2f}, selection regret ${s.selection_regret:,.2f}"
    )


def render_strategy_selection_report(report: StrategySelectionReport) -> str:
    lines: list[str] = ["STRATEGY SELECTION REPORT", ""]

    lines += ["SELECTED STRATEGY / ALTERNATIVES / NO_TRADE RESULTS (aggregate)", ""]
    lines += [f"  Overall: {_fmt_meaningful(report.overall_effectiveness)}", ""]
    lines += ["  By market regime:"]
    for regime in sorted(report.effectiveness_by_market_regime):
        lines += [f"    {regime}: {_fmt_meaningful(report.effectiveness_by_market_regime[regime])}"]
    lines += ["  By volatility regime:"]
    for regime in sorted(report.effectiveness_by_volatility_regime):
        lines += [f"    {regime}: {_fmt_meaningful(report.effectiveness_by_volatility_regime[regime])}"]

    lines += ["", "SELECTED MOST OFTEN", ""]
    for strategy in sorted(report.selection_frequency, key=lambda s: report.selection_frequency[s].times_selected, reverse=True):
        f = report.selection_frequency[strategy]
        lines += [f"  {strategy}: {f.times_selected} time(s) ({f.selection_share:.1%} of all selections)"]
    if not report.selection_frequency:
        lines += ["  (nothing selected yet)"]

    lines += ["", "RISK-ADJUSTED VALUE (expectancy per dollar of capital deployed)", ""]
    for strategy in sorted(report.risk_adjusted_value, key=lambda s: report.risk_adjusted_value[s], reverse=True):
        lines += [f"  {strategy}: {report.risk_adjusted_value[strategy]:.4f}"]

    lines += [
        "", "REDUCES PORTFOLIO RISK: " + (", ".join(report.reduces_portfolio_risk) or "none"),
        "CONSUMES CAPITAL WITHOUT SUFFICIENT BENEFIT: " + (", ".join(report.consumes_capital_without_benefit) or "none"),
        "REGIME-SPECIFIC: " + (", ".join(report.regime_specific) or "none"),
        "EXCESSIVE EXECUTION COSTS: " + (", ".join(report.excessive_execution_costs) or "none"),
    ]

    lines += ["", "DYNAMIC SELECTION VS. A SIMPLER FIXED-STRATEGY APPROACH", ""]
    dvf = report.dynamic_vs_fixed
    if not dvf.dynamic_meaningful_sample:
        lines += [f"  INSUFFICIENT SAMPLE ({dvf.dynamic_sample_size} opportunities) -- cannot yet judge dynamic selection"]
    elif dvf.best_fixed_strategy is None:
        lines += ["  INSUFFICIENT SAMPLE -- no single fixed strategy has a meaningful sample to compare against"]
    else:
        verdict = "OUTPERFORMS" if dvf.dynamic_outperforms_best_fixed else "DOES NOT OUTPERFORM"
        lines += [
            f"  Dynamic selection expectancy: ${dvf.dynamic_expectancy:,.2f} ({dvf.dynamic_sample_size} opportunities)",
            f"  Best fixed-strategy baseline: {dvf.best_fixed_strategy} at ${dvf.best_fixed_strategy_expectancy:,.2f}",
            f"  Verdict: dynamic selection {verdict} the best simple fixed-strategy alternative",
        ]

    return "\n".join(lines)
