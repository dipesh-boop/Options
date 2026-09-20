"""Checkpoint and gate classification for the validation protocol.

**This module never authorizes live trading.** Every `Literal` value
this module can return is spelled out explicitly below; none of them is
"go live," "enable live trading," or anything that could be mistaken for
one — the strongest possible outcome, `PASS_FOR_EXTENDED_VALIDATION`,
names its own next step as *more* validation, not execution. Consistent
with `ARCHITECTURE.md` Section 4: `TradingMode` has no `LIVE` value in
this codebase at all, so there is nothing this module's classification
could even wire into if it tried.

**Never PASS before day 90.** `evaluate_90_day_gate` takes `day: int`
and raises `ValueError` if `day < 90` — this is the literal, structural
enforcement of that requirement: the only function in this package that
can return `PASS_FOR_EXTENDED_VALIDATION` refuses to run at all before
day 90, rather than trusting every caller to remember not to call it
early.

**research_targets is deliberately not a parameter anywhere in this
module.** The 12-15% target existing in `config/validation.yaml` is
read by `src.validation.scorecard`/reporting code for informational
display only — gate classification here depends only on sample
adequacy, drawdown, rule compliance, and risk-adjusted expectancy
(Sharpe / expectancy sign), never on whether the run happened to hit
12-15%. A strategy that returns 6% with a clean process and controlled
risk can PASS; a strategy that returns 20% by violating a rule cannot.
"""
from __future__ import annotations

from typing import Literal

from src.risk.limits import RiskLimitsConfig
from src.validation.protocol import SampleSizeStatus
from src.validation.scorecard import ValidationScorecard

CheckpointClassification = Literal["CONTINUE", "CONTINUE_WITH_WARNING", "HALT_FOR_INVESTIGATION"]
GateClassification = Literal[
    "PASS_FOR_EXTENDED_VALIDATION", "CONDITIONAL_PASS", "EXTEND_VALIDATION", "FAIL_RESEARCH_REVIEW", "HALT",
]

VALID_CHECKPOINT_DAYS = (30, 60)


def evaluate_checkpoint(
    *,
    day: int,
    current_drawdown_pct: float,
    rule_violations: int,
    sharpe: float | None,
    limits: RiskLimitsConfig,
) -> CheckpointClassification:
    """A 30- or 60-day checkpoint never rules the run PASS or FAIL — only
    whether it should keep running unmodified, keep running with a flag
    for the weekly review to watch, or stop for a human to look at
    before day 90. `rule_name`-shaped drawdown thresholds are read from
    the live `RiskLimitsConfig`, never redeclared here (see
    `config/validation.yaml`'s own module docstring for why)."""
    if day not in VALID_CHECKPOINT_DAYS:
        raise ValueError(f"day must be one of {VALID_CHECKPOINT_DAYS}, got {day}")

    if rule_violations > 0 or current_drawdown_pct >= limits.drawdown_halt_pct:
        return "HALT_FOR_INVESTIGATION"
    if current_drawdown_pct >= limits.drawdown_warning_pct or (sharpe is not None and sharpe < 0):
        return "CONTINUE_WITH_WARNING"
    return "CONTINUE"


def _return_category_metric(scorecard: ValidationScorecard, name: str) -> float:
    return float(scorecard.category("RETURN").metrics[name])


def _risk_category_metric(scorecard: ValidationScorecard, name: str) -> float:
    return float(scorecard.category("RISK").metrics[name])


def evaluate_90_day_gate(
    *,
    day: int,
    sample_status: SampleSizeStatus,
    scorecard: ValidationScorecard,
    rule_violations: int,
    current_drawdown_pct: float,
    limits: RiskLimitsConfig,
) -> GateClassification:
    """The 90-day gate. Order of evaluation, each a hard gate over the
    next (never blended into a weighted score):

    1. `day < 90` -> raises (never PASS before day 90).
    2. Any rule violation, or drawdown at/above the Risk Engine's own
       halt threshold -> `HALT`.
    3. `sample_status == INSUFFICIENT_SAMPLE` -> `EXTEND_VALIDATION`
       (the sample-size gate: no amount of good performance overrides
       this).
    4. Negative risk-adjusted expectancy (Sharpe <= 0) with a since-
       inception loss -> `FAIL_RESEARCH_REVIEW`.
    5. `sample_status == MINIMUM_SAMPLE` (50-99 trades) with an
       otherwise clean run -> `CONDITIONAL_PASS`.
    6. `sample_status == PREFERRED_SAMPLE` (100+ trades) with an
       otherwise clean run -> `PASS_FOR_EXTENDED_VALIDATION`.
    """
    if day < 90:
        raise ValueError(f"the 90-day gate cannot be evaluated before day 90 (got day={day})")

    if rule_violations > 0 or current_drawdown_pct >= limits.drawdown_halt_pct:
        return "HALT"

    if sample_status == SampleSizeStatus.INSUFFICIENT_SAMPLE:
        return "EXTEND_VALIDATION"

    sharpe = _risk_category_metric(scorecard, "sharpe")
    since_inception_return = _return_category_metric(scorecard, "since_inception_return")
    if sharpe <= 0 and since_inception_return < 0:
        return "FAIL_RESEARCH_REVIEW"

    if sample_status == SampleSizeStatus.MINIMUM_SAMPLE:
        return "CONDITIONAL_PASS"

    return "PASS_FOR_EXTENDED_VALIDATION"
