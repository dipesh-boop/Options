"""Counterfactual strategy-selection tracking for the validation run
(Step 19A): for every opportunity, record every serious alternative the
Strategy Competition Engine considered (not just the selected one), and
later price what each non-selected alternative would actually have
realized, using the SAME realistic execution assumptions as the
selected trade — never with the benefit of hindsight at construction
time (the alternative's legs/strikes were fixed by `src.strategies
.selector` at decision time, before any outcome was known).

Hypothetical pricing reuses `src.workflows.rejected_trade_review`'s
existing replay machinery (`hypothetical_outcome_from_exit_quotes` /
`hypothetical_outcome_from_settlement`, themselves thin adapters over
`src.backtest.execution`/`assignment`) — a non-selected strategy
alternative and a Risk-Engine-rejected proposal answer the identical
question ("what would this structure's real fill and settlement have
been worth"), so this module builds a `TradeProposal`-shaped adapter
for a `StrategyEvaluation` and calls the same functions, rather than a
second execution model.

"Do NOT judge the Strategy Selector from individual trades" is enforced
exactly like `src.workflows.rejected_trade_review.RejectedTradeStatistics`
already enforces it for rejected trades: `meaningful_sample`/`warning`.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategies.base import StrategyEvaluation, StrategyKind

MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS = 20


@dataclass(frozen=True)
class StrategyAlternativeRecord:
    """One non-selected (or selected) serious alternative, captured at
    decision time -- exactly the fields Step 19A specifies: expected
    value, expected ROC, maximum loss, probability metrics, Greeks,
    capital requirement, Risk Engine result, and the selection/rejection
    reason. Stores the `StrategyEvaluation` itself rather than
    duplicating its fields, so nothing here can drift from what was
    actually evaluated."""

    opportunity_id: str
    evaluation: StrategyEvaluation
    was_selected: bool
    risk_decision: str
    selection_or_rejection_reason: str


@dataclass(frozen=True)
class CounterfactualOutcome:
    opportunity_id: str
    strategy_kind: StrategyKind
    was_selected: bool
    hypothetical_pnl: float
    market_regime: str | None
    volatility_regime: str | None


@dataclass(frozen=True)
class SelectionEffectivenessSummary:
    sample_size: int
    selected_expectancy: float
    average_alternative_expectancy: float
    no_trade_expectancy: float
    selection_regret: float  # average_alternative_expectancy - selected_expectancy (positive = alternatives did better on average)
    risk_adjusted_selection_regret: float | None
    meaningful_sample: bool
    warning: str | None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_selection_effectiveness(
    outcomes: list[CounterfactualOutcome],
    *,
    no_trade_pnl_per_opportunity: float = 0.0,
    min_sample_size: int = MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS,
) -> SelectionEffectivenessSummary:
    """Never judges a single trade -- only an aggregate over
    `outcomes`, one entry per (opportunity, alternative actually
    considered), spanning as many opportunities as the caller supplies."""
    selected = [o.hypothetical_pnl for o in outcomes if o.was_selected]
    alternatives = [o.hypothetical_pnl for o in outcomes if not o.was_selected]
    opportunity_count = len({o.opportunity_id for o in outcomes})

    selected_expectancy = _mean(selected)
    alt_expectancy = _mean(alternatives)
    regret = alt_expectancy - selected_expectancy

    meaningful = opportunity_count >= min_sample_size
    warning = None
    if not meaningful:
        warning = (
            f"only {opportunity_count} opportunity/opportunities sampled, below the {min_sample_size}-opportunity "
            "threshold for a statistically meaningful conclusion -- do not judge the Strategy Selector from this sample"
        )

    return SelectionEffectivenessSummary(
        sample_size=opportunity_count,
        selected_expectancy=selected_expectancy,
        average_alternative_expectancy=alt_expectancy,
        no_trade_expectancy=no_trade_pnl_per_opportunity,
        selection_regret=regret,
        risk_adjusted_selection_regret=None,  # requires each alternative's own max_loss; see summarize_by_regime for the richer breakdown
        meaningful_sample=meaningful,
        warning=warning,
    )


def summarize_by_regime(
    outcomes: list[CounterfactualOutcome], *, key: str, min_sample_size: int = MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS
) -> dict[str, SelectionEffectivenessSummary]:
    """Selection effectiveness bucketed by `market_regime` or
    `volatility_regime` (pass `key="market_regime"` or
    `key="volatility_regime"`) -- each bucket carries its own
    independent `meaningful_sample` flag, since a regime a validation
    run barely saw should never borrow false confidence from another
    regime's larger sample."""
    if key not in ("market_regime", "volatility_regime"):
        raise ValueError("key must be 'market_regime' or 'volatility_regime'")
    buckets: dict[str, list[CounterfactualOutcome]] = {}
    for o in outcomes:
        label = getattr(o, key) or "unspecified"
        buckets.setdefault(label, []).append(o)
    return {label: summarize_selection_effectiveness(bucket, min_sample_size=min_sample_size) for label, bucket in buckets.items()}


@dataclass(frozen=True)
class FixedStrategyBaseline:
    """What portfolio expectancy would have looked like if every
    opportunity this strategy was ever considered for had used it,
    always -- the "simpler fixed-strategy approach" the dynamic
    Strategy Selector is being compared against. Computed only over the
    opportunities where this strategy actually WAS a candidate (its own
    `CounterfactualOutcome` records), never extrapolated to opportunities
    it was never evaluated against."""

    strategy: str
    expectancy: float
    sample_size: int
    meaningful_sample: bool


@dataclass(frozen=True)
class DynamicVsFixedComparison:
    """Answers "is dynamic strategy selection outperforming a simpler
    fixed-strategy approach?" -- `dynamic_outperforms_best_fixed` is
    `None`, never a guessed True/False, whenever either side lacks a
    meaningful sample."""

    dynamic_expectancy: float
    dynamic_sample_size: int
    dynamic_meaningful_sample: bool
    fixed_strategy_baselines: dict[str, FixedStrategyBaseline]
    best_fixed_strategy: str | None
    best_fixed_strategy_expectancy: float | None
    dynamic_outperforms_best_fixed: bool | None


def dynamic_vs_fixed_strategy_comparison(
    outcomes: list[CounterfactualOutcome], *, min_sample_size: int = MIN_SAMPLE_SIZE_FOR_SELECTION_CONCLUSIONS
) -> DynamicVsFixedComparison:
    selected = [o for o in outcomes if o.was_selected]
    dynamic_sample = len({o.opportunity_id for o in selected})
    dynamic_expectancy = _mean([o.hypothetical_pnl for o in selected])
    dynamic_meaningful = dynamic_sample >= min_sample_size

    by_strategy: dict[str, list[CounterfactualOutcome]] = {}
    for o in outcomes:
        by_strategy.setdefault(o.strategy_kind.value, []).append(o)

    baselines: dict[str, FixedStrategyBaseline] = {}
    for strategy, group in by_strategy.items():
        sample = len({o.opportunity_id for o in group})
        baselines[strategy] = FixedStrategyBaseline(
            strategy=strategy, expectancy=_mean([o.hypothetical_pnl for o in group]),
            sample_size=sample, meaningful_sample=sample >= min_sample_size,
        )

    meaningful_baselines = {s: b for s, b in baselines.items() if b.meaningful_sample}
    best_strategy: str | None = None
    best_expectancy: float | None = None
    if meaningful_baselines:
        best_strategy = max(meaningful_baselines, key=lambda s: meaningful_baselines[s].expectancy)
        best_expectancy = meaningful_baselines[best_strategy].expectancy

    outperforms: bool | None = None
    if dynamic_meaningful and best_expectancy is not None:
        outperforms = dynamic_expectancy > best_expectancy

    return DynamicVsFixedComparison(
        dynamic_expectancy=dynamic_expectancy,
        dynamic_sample_size=dynamic_sample,
        dynamic_meaningful_sample=dynamic_meaningful,
        fixed_strategy_baselines=baselines,
        best_fixed_strategy=best_strategy,
        best_fixed_strategy_expectancy=best_expectancy,
        dynamic_outperforms_best_fixed=outperforms,
    )
