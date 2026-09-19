"""Benchmark comparison: SPY total return and a risk-free/Treasury
return over the exact same period a backtest covers — reused
(`src.data.historical.HistoricalBar`), not re-fetched or re-modeled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.data.historical import HistoricalBar, assert_no_lookahead


@dataclass(frozen=True)
class BenchmarkComparison:
    strategy_realistic_return: float
    strategy_theoretical_return: float
    spy_total_return: float
    risk_free_return: float
    excess_return_vs_spy_realistic: float
    excess_return_vs_risk_free_realistic: float


def spy_total_return(bars: list[HistoricalBar], start: date, end: date) -> float:
    """Simple total return from close-to-close over [start, end] —
    `assert_no_lookahead` is run against `end` first, so a benchmark
    figure can never be computed from a bar the strategy itself
    wouldn't have been allowed to see yet."""
    assert_no_lookahead(bars, end)
    in_range = sorted((b for b in bars if start <= b.bar_date <= end), key=lambda b: b.bar_date)
    if len(in_range) < 2:
        raise ValueError(f"need at least two bars in [{start.isoformat()}, {end.isoformat()}] to compute a return")
    return in_range[-1].close / in_range[0].close - 1.0


def risk_free_return(annual_rate: float, start: date, end: date) -> float:
    """Simple accrual of a constant annualized rate over the period —
    a deliberately simple Treasury-return stand-in; a real yield-curve
    model is future work, not assumed here."""
    if annual_rate < 0:
        raise ValueError("annual_rate cannot be negative")
    years = (end - start).days / 365.0
    if years <= 0:
        raise ValueError("end must be after start")
    return (1.0 + annual_rate) ** years - 1.0


def compare_to_benchmarks(
    *,
    strategy_realistic_return: float,
    strategy_theoretical_return: float,
    spy_bars: list[HistoricalBar],
    risk_free_annual_rate: float,
    start: date,
    end: date,
) -> BenchmarkComparison:
    spy_return = spy_total_return(spy_bars, start, end)
    rf_return = risk_free_return(risk_free_annual_rate, start, end)
    return BenchmarkComparison(
        strategy_realistic_return=strategy_realistic_return,
        strategy_theoretical_return=strategy_theoretical_return,
        spy_total_return=spy_return,
        risk_free_return=rf_return,
        excess_return_vs_spy_realistic=strategy_realistic_return - spy_return,
        excess_return_vs_risk_free_realistic=strategy_realistic_return - rf_return,
    )
