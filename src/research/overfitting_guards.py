"""Overfitting protection: the "OVERFITTING PROTECTION" section of Step
14. Tracks hypothesis counts and raises deterministic, Python-computed
warnings for multiple-testing bias, parameter mining, small samples,
regime dependence, and survivorship bias — the Strategy Research Agent
(LLM) only ever *reads* this module's output as reference data; it
cannot silence, adjust, or override a warning this module raises.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.research.hypothesis import HypothesisRegistry
from src.research.performance_breakdown import BucketStats

# Plain module constants, not hardcoded inline magic numbers — same
# "configurable, named, not buried in an expression" idiom
# `src.llm.devils_advocate`'s staleness thresholds already establish.
MIN_TRADES_FOR_SIGNIFICANCE = 30
MAX_FAMILY_VARIATIONS_BEFORE_WARNING = 5
REGIME_CONCENTRATION_WARNING_THRESHOLD = 0.80
MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK = 10
MAX_SURVIVOR_RATIO_BEFORE_WARNING = 0.10


@dataclass(frozen=True)
class OverfittingGuardResult:
    hypotheses_tested: int
    hypotheses_rejected: int
    surviving_validation: int
    surviving_out_of_sample: int
    warnings: tuple[str, ...]


def check_small_sample(trade_count: int, *, min_trades: int = MIN_TRADES_FOR_SIGNIFICANCE) -> str | None:
    if trade_count < min_trades:
        return (
            f"small sample: only {trade_count} trade(s) support this hypothesis, "
            f"below the {min_trades}-trade threshold for a statistically meaningful read"
        )
    return None


def check_regime_dependence(regime_buckets: tuple[BucketStats, ...], *, threshold: float = REGIME_CONCENTRATION_WARNING_THRESHOLD) -> str | None:
    """Warns when almost all of the profit came from a single market
    regime bucket — a strategy that only worked in one regime is a
    regime-dependent result, not necessarily a durable edge."""
    total_profit = sum(b.total_pnl for b in regime_buckets if b.total_pnl > 0)
    if total_profit <= 0 or not regime_buckets:
        return None
    dominant = max(regime_buckets, key=lambda b: max(b.total_pnl, 0.0))
    concentration = max(dominant.total_pnl, 0.0) / total_profit
    if concentration >= threshold:
        return (
            f"regime dependence: {concentration:.0%} of gross profit came from the "
            f"{dominant.bucket!r} regime bucket alone — this result may not generalize "
            "to other regimes"
        )
    return None


def check_parameter_mining(
    registry: HypothesisRegistry, parameter_family_key: str, *, max_variations: int = MAX_FAMILY_VARIATIONS_BEFORE_WARNING
) -> str | None:
    """Warns when a family of near-duplicate parameter variations (same
    underlying idea, delta/DTE nudged slightly) has been tested many
    times — the concrete guard behind "do not repeatedly test minor
    parameter changes until something profitable appears.\""""
    count = registry.family_count(parameter_family_key)
    if count > max_variations:
        return (
            f"parameter mining: {count} variations of the {parameter_family_key!r} family "
            f"have now been tested, above the {max_variations}-variation threshold — "
            "consider whether this is genuine research or a search for a lucky parameter"
        )
    return None


def check_multiple_testing_bias(
    registry: HypothesisRegistry,
    *,
    min_tested: int = MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK,
    max_survivor_ratio: float = MAX_SURVIVOR_RATIO_BEFORE_WARNING,
) -> str | None:
    """Warns when many hypotheses have been tested this session relative
    to how few survived out-of-sample — a low survivor ratio across a
    large number of trials is exactly the pattern multiple-testing
    (false discovery) produces even when no single hypothesis was
    individually p-hacked."""
    tested = registry.tested_count()
    if tested < min_tested:
        return None
    survived = registry.count_by_status("survived_out_of_sample")
    ratio = survived / tested
    if ratio < max_survivor_ratio:
        return (
            f"multiple-testing bias: {tested} hypotheses tested this session, only "
            f"{survived} ({ratio:.0%}) survived out-of-sample — at this volume of trials, "
            "some survivors are expected by chance alone"
        )
    return None


def survivorship_bias_note() -> str:
    """A standing caution, always included — matches the same honesty
    `src.backtest.engine`'s module docstring already gives for
    survivorship bias: this platform has no confirmed
    survivorship-bias-free historical vendor, so any backtest result is
    only as good as whatever `HistoricalOptionChainProvider` implementation
    supplied its data."""
    return (
        "survivorship bias: no confirmed survivorship-bias-free historical data vendor is "
        "wired up yet (see ARCHITECTURE.md §12) — a result computed only over tickers that "
        "still exist today may look better than it would have looked live"
    )


def run_overfitting_guards(
    registry: HypothesisRegistry,
    *,
    hypothesis_id: str,
    trade_count: int,
    regime_buckets: tuple[BucketStats, ...],
    parameter_family_key: str,
) -> OverfittingGuardResult:
    """Assembles every guard's output for one hypothesis into the single
    object `src.llm.strategy_research` passes to the model as read-only
    context."""
    registry.get(hypothesis_id)  # raises UnknownHypothesisError if not registered

    warnings: list[str] = []
    for check in (
        check_small_sample(trade_count),
        check_regime_dependence(regime_buckets),
        check_parameter_mining(registry, parameter_family_key),
        check_multiple_testing_bias(registry),
    ):
        if check:
            warnings.append(check)
    warnings.append(survivorship_bias_note())

    return OverfittingGuardResult(
        hypotheses_tested=registry.tested_count(),
        hypotheses_rejected=registry.count_by_status("rejected"),
        surviving_validation=registry.count_by_status("validated") + registry.count_by_status("out_of_sample_tested") + registry.count_by_status("survived_out_of_sample"),
        surviving_out_of_sample=registry.count_by_status("survived_out_of_sample"),
        warnings=tuple(warnings),
    )
