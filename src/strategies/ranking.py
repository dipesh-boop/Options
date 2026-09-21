"""The Strategy Selector's ranking function, split out from
`src.strategies.comparison` (which owns the metrics table,
`ComparisonRow`) so the *ranking rule itself* is one small, independently
testable module — the file Step 14B names explicitly, separate from the
comparison engine.

`risk_adjusted_score` is expected value per dollar of defined risk
(`expected_value / maximum_loss`), not raw expected return — the literal
antidote to Step 19A/14B's own worked example: a 25%-return/large-
max-loss candidate must not automatically outrank a 14%-return/small-
max-loss one. `rank_candidates` never collapses anything into just this
one number for reporting — callers (`src.strategies.selector`) still
carry the full `ComparisonRow` table alongside whatever this module
ranks.
"""
from __future__ import annotations

from src.strategies.base import StrategyEvaluation


def risk_adjusted_score(evaluation: StrategyEvaluation) -> float:
    """Expected value per dollar of defined risk. `maximum_loss <= 0`
    (a theoretical zero-risk structure) returns the raw expected value
    unscaled rather than dividing by zero -- a documented edge case, not
    expected to occur for any of this platform's approved strategies."""
    if evaluation.maximum_loss <= 0:
        return evaluation.expected_value
    return evaluation.expected_value / evaluation.maximum_loss


def rank_candidates(evaluations: list[StrategyEvaluation]) -> list[StrategyEvaluation]:
    """Highest `risk_adjusted_score` first. This ranking alone is never
    the final selection -- `src.strategies.selector.select_best_or_no_trade`
    still requires the top candidate to clear the NO_TRADE hurdle and to
    have actually cleared the Risk Engine before it can be chosen."""
    return sorted(evaluations, key=risk_adjusted_score, reverse=True)
