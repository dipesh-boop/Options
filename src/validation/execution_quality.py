"""FIDELITY EXECUTION QUALITY and options-specific execution metrics for
a whole validation run.

The Fidelity slippage half is a thin wrapper over
`src.workflows.execution_quality.summarize_slippage` (Step 16,
unchanged) applied to a validation run's accumulated
`FidelitySlippageRecord`s instead of one week's. Options execution
metrics (fill rate, assignment rate, early-close rate) are genuinely
new here — Step 16 never needed them because a single week rarely has
enough closed trades to make a rate meaningful; a 90-day run does.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.simulator import TradeRecord
from src.workflows.execution_quality import FidelitySlippageRecord, SlippageSummary, summarize_slippage

_ASSIGNMENT_REASONS = frozenset({"assignment", "exercise"})
_EARLY_CLOSE_REASONS = frozenset({"profit_target", "dte_management"})


def summarize_validation_execution_quality(records: list[FidelitySlippageRecord]) -> SlippageSummary:
    """Reused unmodified — see `src.workflows.execution_quality
    .summarize_slippage`."""
    return summarize_slippage(records)


@dataclass(frozen=True)
class OptionsExecutionMetrics:
    total_closed_trades: int
    assignment_count: int
    assignment_rate: float
    early_close_count: int
    early_close_rate: float
    expiration_otm_count: int
    expiration_otm_rate: float
    attempted_trades: int | None
    fill_rate: float | None


def compute_options_execution_metrics(
    trades: list[TradeRecord], *, attempted_trades: int | None = None
) -> OptionsExecutionMetrics:
    """`fill_rate` (filled / attempted) needs a denominator this store
    doesn't itself track — `attempted_trades` is caller-supplied
    (e.g. from `src.validation.session.ValidationStore` plus whatever
    counted proposals the Risk Engine rejected before a fill was even
    attempted), exactly like `src.workflows.morning_scan`'s honest
    "not tracked" fields for data this module has no way to derive on
    its own. When omitted, `fill_rate` is `None`, never fabricated."""
    total = len(trades)
    if total == 0:
        return OptionsExecutionMetrics(
            total_closed_trades=0, assignment_count=0, assignment_rate=0.0,
            early_close_count=0, early_close_rate=0.0, expiration_otm_count=0, expiration_otm_rate=0.0,
            attempted_trades=attempted_trades, fill_rate=None,
        )
    assignment_count = sum(1 for t in trades if t.close_reason in _ASSIGNMENT_REASONS)
    early_close_count = sum(1 for t in trades if t.close_reason in _EARLY_CLOSE_REASONS)
    expiration_otm_count = sum(1 for t in trades if t.close_reason == "expiration_otm")
    fill_rate = (total / attempted_trades) if attempted_trades else None
    return OptionsExecutionMetrics(
        total_closed_trades=total,
        assignment_count=assignment_count,
        assignment_rate=assignment_count / total,
        early_close_count=early_close_count,
        early_close_rate=early_close_count / total,
        expiration_otm_count=expiration_otm_count,
        expiration_otm_rate=expiration_otm_count / total,
        attempted_trades=attempted_trades,
        fill_rate=fill_rate,
    )
