"""DECISION QUALITY section of `/weekly-review` (Step 16): classify each
closed trade into one of four quadrants — "Do NOT judge decision quality
solely by P&L."

The concrete mechanism behind that instruction: "decision quality" is
computed entirely from facts that were known and computed *before* the
trade's outcome was known (the Risk Engine's decision, the Devil's
Advocate's verdict, Python Quant's stated probability of profit) —
never from `realistic_pnl`. "Outcome quality" is the only thing P&L
decides. A trade can be a good decision that lost money (bad luck within
its own stated probability of profit) or a bad decision that made money
(good luck despite a process failure) — both are meaningful, distinct
findings this classifier is built specifically to preserve rather than
collapse into "it made money, so it must have been right."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.risk.reason_codes import RiskDecision

DecisionQualityLabel = Literal[
    "good_decision_good_outcome",
    "good_decision_bad_outcome",
    "bad_decision_good_outcome",
    "bad_decision_bad_outcome",
]

DEFAULT_MIN_PROBABILITY_OF_PROFIT = 0.50
_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)
_ACCEPTABLE_DA_VERDICTS = ("PASS", "CAUTION")


@dataclass(frozen=True)
class DecisionQualityInputs:
    """Every field here must be the ex-ante (decision-time) record of
    what was known and decided — never something recomputed with the
    benefit of hindsight."""

    risk_decision: RiskDecision
    devils_advocate_verdict: str
    probability_of_profit: float
    realistic_pnl: float


@dataclass(frozen=True)
class DecisionQualityResult:
    label: DecisionQualityLabel
    good_decision: bool
    good_outcome: bool


def was_good_decision(inputs: DecisionQualityInputs, *, min_probability_of_profit: float = DEFAULT_MIN_PROBABILITY_OF_PROFIT) -> bool:
    """A decision was "good" if the process that produced it was sound
    at the time — Risk-Engine-approved (outright or resized, never
    rejected/halted), not REJECTed by the Devil's Advocate, and backed
    by a stated probability of profit at or above the threshold. None of
    these three facts depends on what actually happened afterward."""
    if inputs.risk_decision not in _ACCEPTABLE_RISK_DECISIONS:
        return False
    if inputs.devils_advocate_verdict not in _ACCEPTABLE_DA_VERDICTS:
        return False
    if inputs.probability_of_profit < min_probability_of_profit:
        return False
    return True


def was_good_outcome(inputs: DecisionQualityInputs) -> bool:
    return inputs.realistic_pnl > 0


def classify_decision_quality(inputs: DecisionQualityInputs, *, min_probability_of_profit: float = DEFAULT_MIN_PROBABILITY_OF_PROFIT) -> DecisionQualityResult:
    good_decision = was_good_decision(inputs, min_probability_of_profit=min_probability_of_profit)
    good_outcome = was_good_outcome(inputs)
    if good_decision and good_outcome:
        label: DecisionQualityLabel = "good_decision_good_outcome"
    elif good_decision and not good_outcome:
        label = "good_decision_bad_outcome"
    elif not good_decision and good_outcome:
        label = "bad_decision_good_outcome"
    else:
        label = "bad_decision_bad_outcome"
    return DecisionQualityResult(label=label, good_decision=good_decision, good_outcome=good_outcome)


@dataclass(frozen=True)
class DecisionQualitySummary:
    counts: dict[DecisionQualityLabel, int]
    total: int

    def fraction(self, label: DecisionQualityLabel) -> float:
        if self.total == 0:
            return 0.0
        return self.counts.get(label, 0) / self.total


_ALL_LABELS: tuple[DecisionQualityLabel, ...] = (
    "good_decision_good_outcome", "good_decision_bad_outcome", "bad_decision_good_outcome", "bad_decision_bad_outcome",
)


def summarize_decision_quality(results: list[DecisionQualityResult]) -> DecisionQualitySummary:
    counts: dict[DecisionQualityLabel, int] = {label: 0 for label in _ALL_LABELS}
    for r in results:
        counts[r.label] += 1
    return DecisionQualitySummary(counts=counts, total=len(results))
