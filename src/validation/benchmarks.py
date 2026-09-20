"""SPY / risk-free / cash benchmark comparison over the validation
period's exact dates. Wraps `src.backtest.benchmark`
(`spy_total_return`/`risk_free_return`) rather than a second
implementation — the cash comparison (a flat 0% return over the period)
is the one genuinely new figure, since holding cash is this platform's
own "a valid position" baseline (`src.workflows.morning_scan`'s NO TRADE
report already says so in plain language for a single scan; this is the
same idea applied over a whole validation period).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.backtest.benchmark import risk_free_return, spy_total_return
from src.data.historical import HistoricalBar


@dataclass(frozen=True)
class ValidationBenchmarkComparison:
    strategy_return: float
    spy_return: float
    risk_free_return: float
    cash_return: float
    excess_vs_spy: float
    excess_vs_risk_free: float
    excess_vs_cash: float
    start: date
    end: date


def compare_against_benchmarks(
    *,
    strategy_return: float,
    spy_bars: list[HistoricalBar],
    risk_free_annual_rate: float,
    start: date,
    end: date,
) -> ValidationBenchmarkComparison:
    spy_return = spy_total_return(spy_bars, start, end)
    rf_return = risk_free_return(risk_free_annual_rate, start, end)
    cash_return = 0.0
    return ValidationBenchmarkComparison(
        strategy_return=strategy_return,
        spy_return=spy_return,
        risk_free_return=rf_return,
        cash_return=cash_return,
        excess_vs_spy=strategy_return - spy_return,
        excess_vs_risk_free=strategy_return - rf_return,
        excess_vs_cash=strategy_return - cash_return,
        start=start,
        end=end,
    )
