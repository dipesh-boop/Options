"""Fidelity operational practicality: the "FIDELITY OPERATIONAL
PRACTICALITY" section of Step 14. Every input here is a Python-supplied
figure or categorical tier describing the candidate strategy's
operational demands — never something the LLM estimates — and the
LOW/MEDIUM/HIGH/INCOMPATIBLE classification is a deterministic function
of those inputs, the same "Python computes the classification, the LLM
only narrates it" relationship `src.risk` has with every other agent
role in this codebase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["low", "medium", "high"]

FidelityPracticalityRating = Literal["LOW", "MEDIUM", "HIGH", "INCOMPATIBLE"]

_TIER_POINTS: dict[Tier, int] = {"low": 0, "medium": 1, "high": 2}
# liquidity is the opposite sense of the other tiers -- "low" liquidity is
# what adds burden (harder to get a fair manual fill), not "high" liquidity.
_LIQUIDITY_BURDEN_POINTS: dict[Tier, int] = {"high": 0, "medium": 1, "low": 2}

# Weighted-score thresholds and per-factor point cutoffs — plain module
# constants, never buried inline, so a future policy change is a
# one-line edit here rather than a hunt through the scoring function.
LOW_BURDEN_MAX_SCORE = 2
MEDIUM_BURDEN_MAX_SCORE = 5

TRADES_PER_WEEK_MEDIUM = 5.0
TRADES_PER_WEEK_HIGH = 10.0
ADJUSTMENTS_PER_WEEK_MEDIUM = 2.0
ADJUSTMENTS_PER_WEEK_HIGH = 5.0
NUM_LEGS_BURDEN_THRESHOLD = 2  # a strategy with more legs than this (e.g. an iron condor) adds burden
ROLLING_FREQUENCY_PER_MONTH_THRESHOLD = 4.0


@dataclass(frozen=True)
class FidelityPracticalityInputs:
    trades_per_week: float
    adjustments_per_week: float
    num_legs: int
    rolling_frequency_per_month: float
    liquidity: Tier  # "high" = good/deep liquidity, the opposite sense of the others
    monitoring_requirement: Tier
    assignment_complexity: Tier
    time_sensitivity: Tier
    requires_subsecond_decisions: bool = False
    requires_constant_intraday_adjustment: bool = False
    requires_high_frequency_execution: bool = False

    def __post_init__(self) -> None:
        if self.trades_per_week < 0 or self.adjustments_per_week < 0 or self.rolling_frequency_per_month < 0:
            raise ValueError("weekly/monthly frequency inputs cannot be negative")
        if self.num_legs < 1:
            raise ValueError("num_legs must be at least 1")


@dataclass(frozen=True)
class FidelityPracticalityResult:
    rating: FidelityPracticalityRating
    burden_score: int
    reasons: tuple[str, ...]
    hard_rejected: bool


def is_hard_incompatible(inputs: FidelityPracticalityInputs) -> bool:
    """"Reject strategies requiring: high-frequency execution, sub-second
    decisions, constant intraday adjustment" — a hard boolean gate,
    independent of the weighted burden score, since these three are
    disqualifying on their own regardless of how low everything else
    scores."""
    return inputs.requires_subsecond_decisions or inputs.requires_constant_intraday_adjustment or inputs.requires_high_frequency_execution


def compute_burden_score(inputs: FidelityPracticalityInputs) -> int:
    score = 0
    if inputs.trades_per_week >= TRADES_PER_WEEK_HIGH:
        score += 2
    elif inputs.trades_per_week >= TRADES_PER_WEEK_MEDIUM:
        score += 1

    if inputs.adjustments_per_week >= ADJUSTMENTS_PER_WEEK_HIGH:
        score += 2
    elif inputs.adjustments_per_week >= ADJUSTMENTS_PER_WEEK_MEDIUM:
        score += 1

    if inputs.num_legs > NUM_LEGS_BURDEN_THRESHOLD:
        score += 1

    if inputs.rolling_frequency_per_month >= ROLLING_FREQUENCY_PER_MONTH_THRESHOLD:
        score += 1

    score += _TIER_POINTS[inputs.monitoring_requirement]
    score += _TIER_POINTS[inputs.assignment_complexity]
    score += _TIER_POINTS[inputs.time_sensitivity]
    score += _LIQUIDITY_BURDEN_POINTS[inputs.liquidity]

    return score


def classify_fidelity_practicality(inputs: FidelityPracticalityInputs) -> FidelityPracticalityResult:
    """Prefer LOW; MEDIUM is workable; HIGH is discouraged but not
    forbidden; INCOMPATIBLE is a hard reject regardless of score."""
    if is_hard_incompatible(inputs):
        reasons = []
        if inputs.requires_subsecond_decisions:
            reasons.append("requires sub-second decisions")
        if inputs.requires_constant_intraday_adjustment:
            reasons.append("requires constant intraday adjustment")
        if inputs.requires_high_frequency_execution:
            reasons.append("requires high-frequency execution")
        return FidelityPracticalityResult(
            rating="INCOMPATIBLE", burden_score=compute_burden_score(inputs), reasons=tuple(reasons), hard_rejected=True
        )

    score = compute_burden_score(inputs)
    if score <= LOW_BURDEN_MAX_SCORE:
        rating: FidelityPracticalityRating = "LOW"
    elif score <= MEDIUM_BURDEN_MAX_SCORE:
        rating = "MEDIUM"
    else:
        rating = "HIGH"

    reasons = (f"burden score {score} ({inputs.trades_per_week:.1f} trades/week, "
               f"{inputs.adjustments_per_week:.1f} adjustments/week, {inputs.num_legs} leg(s), "
               f"monitoring={inputs.monitoring_requirement}, assignment_complexity={inputs.assignment_complexity})",)
    return FidelityPracticalityResult(rating=rating, burden_score=score, reasons=reasons, hard_rejected=False)
