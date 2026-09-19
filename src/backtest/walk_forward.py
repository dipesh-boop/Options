"""Walk-forward testing: TRAINING / VALIDATION / OUT-OF-SAMPLE period
splitting, rolling forward — e.g. train 2015-2019, validate 2020-2021,
out-of-sample 2022, then roll the whole window forward.

**Never optimize using the out-of-sample period.** This module doesn't
just say that — there is no parameter anywhere in `run_walk_forward`
through which an optimizer, objective function, or parameter-search
callback could be passed at all. A strategy's configuration
(`BacktestConfig`, `EntrySignal`s) must already be fixed before calling
this module; `run_walk_forward` runs that same, already-decided
configuration across all three windows and reports each window's result
independently — it has no mechanism to feed a later window's result
back into an earlier decision, because it makes no decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.backtest.engine import BacktestConfig, run_backtest
from src.backtest.simulator import EntrySignal, PortfolioState
from src.backtest.engine import QuoteLookup


@dataclass(frozen=True)
class WalkForwardWindow:
    start: date
    end: date


@dataclass(frozen=True)
class WalkForwardSplit:
    training: WalkForwardWindow
    validation: WalkForwardWindow
    out_of_sample: WalkForwardWindow


def generate_walk_forward_splits(
    *,
    overall_start: date,
    overall_end: date,
    train_years: int,
    validate_years: int,
    out_of_sample_years: int,
    step_years: int | None = None,
) -> list[WalkForwardSplit]:
    """Rolling `[train][validate][out_of_sample]` windows spanning
    `[overall_start, overall_end]`. `step_years` defaults to
    `out_of_sample_years` (each new split's out-of-sample period starts
    exactly where the previous one ended) — the natural "roll forward"
    reading of the spec's own example."""
    if train_years <= 0 or validate_years <= 0 or out_of_sample_years <= 0:
        raise ValueError("train_years, validate_years, and out_of_sample_years must all be positive")
    step = step_years if step_years is not None else out_of_sample_years
    if step <= 0:
        raise ValueError("step_years must be positive")

    splits: list[WalkForwardSplit] = []
    train_start = overall_start
    while True:
        train_end = train_start.replace(year=train_start.year + train_years)
        validate_end = train_end.replace(year=train_end.year + validate_years)
        oos_end = validate_end.replace(year=validate_end.year + out_of_sample_years)
        if oos_end > overall_end:
            break
        splits.append(
            WalkForwardSplit(
                training=WalkForwardWindow(train_start, train_end),
                validation=WalkForwardWindow(train_end, validate_end),
                out_of_sample=WalkForwardWindow(validate_end, oos_end),
            )
        )
        train_start = train_start.replace(year=train_start.year + step)
    return splits


@dataclass(frozen=True)
class WalkForwardWindowResult:
    window: WalkForwardWindow
    state: PortfolioState


@dataclass(frozen=True)
class WalkForwardResult:
    split: WalkForwardSplit
    training: WalkForwardWindowResult
    validation: WalkForwardWindowResult
    out_of_sample: WalkForwardWindowResult


def _filter_entries(entries: list[EntrySignal], window: WalkForwardWindow) -> list[EntrySignal]:
    return [e for e in entries if window.start <= e.entry_date < window.end]


def _filter_days(trading_days: list[date], window: WalkForwardWindow) -> list[date]:
    return sorted(d for d in trading_days if window.start <= d < window.end)


def run_walk_forward(
    split: WalkForwardSplit,
    entries: list[EntrySignal],
    quote_lookup: QuoteLookup,
    trading_days: list[date],
    config: BacktestConfig,
) -> WalkForwardResult:
    """Runs the exact same `config` (already fixed by the caller, before
    this function is ever called) independently across all three
    windows of `split`. Each window starts from a fresh `PortfolioState`
    — no state, and no parameter choice, carries over from training or
    validation into the out-of-sample run."""
    results = {}
    for name, window in (("training", split.training), ("validation", split.validation), ("out_of_sample", split.out_of_sample)):
        window_entries = _filter_entries(entries, window)
        window_days = _filter_days(trading_days, window)
        state = run_backtest(window_entries, quote_lookup, window_days, config)
        results[name] = WalkForwardWindowResult(window=window, state=state)

    return WalkForwardResult(
        split=split,
        training=results["training"],
        validation=results["validation"],
        out_of_sample=results["out_of_sample"],
    )
