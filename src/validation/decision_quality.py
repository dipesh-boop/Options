"""DECISION QUALITY and continued rejected-trade tracking for a whole
validation run — combined and per-strategy, "never judged from P&L
alone."

This is a thin orchestration layer over `src.workflows.decision_quality`
(the four-quadrant classifier built in Step 16, unchanged) and
`src.workflows.rejected_trade_review` (the statistical rejected-trade
summarizer, also unchanged) — no new classification logic exists here,
only the validation-period-scoped, per-strategy-and-combined aggregation
Step 19 additionally asks for.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.simulator import TradeRecord
from src.risk.reason_codes import RiskDecision
from src.workflows.decision_quality import (
    DecisionQualityInputs,
    DecisionQualityResult,
    DecisionQualitySummary,
    classify_decision_quality,
    summarize_decision_quality,
)
from src.workflows.rejected_trade_review import (
    HypotheticalOutcome,
    RejectedTradeStatistics,
    summarize_rejected_outcomes,
)


@dataclass(frozen=True)
class ValidationDecisionRecord:
    """One closed trade plus the ex-ante facts known at entry — the
    trade-level input `classify_decision_quality` needs, bundled with
    the `TradeRecord` itself so per-strategy grouping (`trade.strategy`)
    doesn't require a second lookup."""

    trade: TradeRecord
    risk_decision: RiskDecision
    devils_advocate_verdict: str
    probability_of_profit: float


def classify_validation_trade(record: ValidationDecisionRecord, *, min_probability_of_profit: float) -> DecisionQualityResult:
    inputs = DecisionQualityInputs(
        risk_decision=record.risk_decision,
        devils_advocate_verdict=record.devils_advocate_verdict,
        probability_of_profit=record.probability_of_profit,
        realistic_pnl=record.trade.realistic_pnl,
    )
    return classify_decision_quality(inputs, min_probability_of_profit=min_probability_of_profit)


@dataclass(frozen=True)
class ValidationDecisionQualityReport:
    combined: DecisionQualitySummary
    by_strategy: dict[str, DecisionQualitySummary]


def summarize_validation_decision_quality(
    records: list[ValidationDecisionRecord], *, min_probability_of_profit: float
) -> ValidationDecisionQualityReport:
    """Combined and per-strategy summaries, kept as separate dict
    entries — never averaged or blended into one summary, the same
    "never collapsed" discipline `src.validation.scorecard` applies at
    the whole-report level."""
    all_results = [classify_validation_trade(r, min_probability_of_profit=min_probability_of_profit) for r in records]
    combined = summarize_decision_quality(all_results)

    by_strategy_records: dict[str, list[DecisionQualityResult]] = {}
    for record, result in zip(records, all_results):
        by_strategy_records.setdefault(record.trade.strategy.value, []).append(result)
    by_strategy = {strategy: summarize_decision_quality(results) for strategy, results in by_strategy_records.items()}

    return ValidationDecisionQualityReport(combined=combined, by_strategy=by_strategy)


def summarize_validation_rejected_trades(
    outcomes: list[HypotheticalOutcome], *, min_sample_size: int
) -> RejectedTradeStatistics:
    """Continued rejected-trade tracking is purely statistical — this
    reuses `summarize_rejected_outcomes` unmodified, which already
    refuses to let a single rejected winner (or loser) read as proof:
    `meaningful_sample`/`warning` fire the same way here as they do in a
    single `/weekly-review` run, just over the whole validation window's
    accumulated sample instead of one week's."""
    return summarize_rejected_outcomes(outcomes, min_sample_size=min_sample_size)
