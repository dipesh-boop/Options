"""Performance metrics computed from a backtest's equity curve and trade
log. Every formula here is a standard, textbook definition — chosen
deliberately, in the same spirit as `src.quant`'s "independently
verifiable mathematical cases" — so each one can be hand-checked against
a small, exact synthetic example in tests, not just trusted because the
code runs.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from src.backtest.simulator import TradeRecord

TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True)
class PerformanceMetrics:
    cagr: float
    annual_volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float  # positive fraction, e.g. 0.18 for an 18% drawdown
    calmar: float
    win_rate: float
    average_winner: float
    average_loser: float  # positive number (magnitude of the average loss)
    profit_factor: float
    expectancy: float
    var_95: float  # positive fraction: the loss at the 5th percentile of period returns
    cvar_95: float  # positive fraction: mean loss beyond the VaR threshold
    worst_month: float
    worst_year: float
    longest_drawdown_days: int
    capital_utilization: float
    trade_count: int
    average_holding_period_days: float


def _periodic_returns(equity_curve: list[tuple[date, float]]) -> list[float]:
    if len(equity_curve) < 2:
        return []
    returns = []
    for (_, prev), (_, curr) in zip(equity_curve, equity_curve[1:]):
        if prev <= 0:
            raise ValueError("equity curve contains a non-positive value; cannot compute a return")
        returns.append(curr / prev - 1.0)
    return returns


def _years_between(equity_curve: list[tuple[date, float]]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    days = (equity_curve[-1][0] - equity_curve[0][0]).days
    return days / 365.0


def cagr(equity_curve: list[tuple[date, float]]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    years = _years_between(equity_curve)
    if years <= 0:
        return 0.0
    start, end = equity_curve[0][1], equity_curve[-1][1]
    if start <= 0:
        raise ValueError("starting equity must be positive")
    return (end / start) ** (1.0 / years) - 1.0


def annual_volatility(equity_curve: list[tuple[date, float]]) -> float:
    returns = _periodic_returns(equity_curve)
    if len(returns) < 2:
        return 0.0
    years = _years_between(equity_curve)
    if years <= 0:
        return 0.0
    periods_per_year = len(returns) / years
    return _stdev(returns) * math.sqrt(periods_per_year)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def sharpe_ratio(equity_curve: list[tuple[date, float]], risk_free_annual_rate: float) -> float:
    returns = _periodic_returns(equity_curve)
    if len(returns) < 2:
        return 0.0
    years = _years_between(equity_curve)
    if years <= 0:
        return 0.0
    periods_per_year = len(returns) / years
    risk_free_periodic = (1.0 + risk_free_annual_rate) ** (1.0 / periods_per_year) - 1.0
    excess = [r - risk_free_periodic for r in returns]
    std = _stdev(returns)
    if std == 0:
        return 0.0
    return (_mean(excess) / std) * math.sqrt(periods_per_year)


def sortino_ratio(equity_curve: list[tuple[date, float]], risk_free_annual_rate: float) -> float:
    returns = _periodic_returns(equity_curve)
    if len(returns) < 2:
        return 0.0
    years = _years_between(equity_curve)
    if years <= 0:
        return 0.0
    periods_per_year = len(returns) / years
    risk_free_periodic = (1.0 + risk_free_annual_rate) ** (1.0 / periods_per_year) - 1.0
    excess = [r - risk_free_periodic for r in returns]
    downside = [min(e, 0.0) ** 2 for e in excess]
    downside_dev = math.sqrt(sum(downside) / len(downside))
    if downside_dev == 0:
        return 0.0
    return (_mean(excess) / downside_dev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: list[tuple[date, float]]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    worst = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, 1.0 - equity / peak)
    return worst


def longest_drawdown_days(equity_curve: list[tuple[date, float]]) -> int:
    """The longest stretch (in days) equity spent below a prior peak
    before making a new high — distinct from `max_drawdown`'s *depth*,
    this measures *duration*."""
    if not equity_curve:
        return 0
    peak = equity_curve[0][1]
    peak_date = equity_curve[0][0]
    longest = 0
    for as_of, equity in equity_curve:
        if equity >= peak:
            peak = equity
            peak_date = as_of
        else:
            longest = max(longest, (as_of - peak_date).days)
    return longest


def calmar_ratio(equity_curve: list[tuple[date, float]]) -> float:
    dd = max_drawdown(equity_curve)
    if dd == 0:
        return 0.0
    return cagr(equity_curve) / dd


def _monthly_returns(equity_curve: list[tuple[date, float]]) -> list[float]:
    return _period_returns_by_key(equity_curve, key=lambda d: (d.year, d.month))


def _yearly_returns(equity_curve: list[tuple[date, float]]) -> list[float]:
    return _period_returns_by_key(equity_curve, key=lambda d: d.year)


def _period_returns_by_key(equity_curve: list[tuple[date, float]], key) -> list[float]:
    if not equity_curve:
        return []
    buckets: dict = defaultdict(list)
    for as_of, equity in equity_curve:
        buckets[key(as_of)].append(equity)
    returns = []
    for values in buckets.values():
        if values[0] > 0:
            returns.append(values[-1] / values[0] - 1.0)
    return returns


def worst_month(equity_curve: list[tuple[date, float]]) -> float:
    returns = _monthly_returns(equity_curve)
    return min(returns) if returns else 0.0


def worst_year(equity_curve: list[tuple[date, float]]) -> float:
    returns = _yearly_returns(equity_curve)
    return min(returns) if returns else 0.0


def historical_var(equity_curve: list[tuple[date, float]], confidence: float = 0.95) -> float:
    """Historical VaR: the loss at the `confidence` percentile of the
    empirical periodic-return distribution, reported as a positive
    fraction. E.g. `var_95=0.03` means the worst 5% of periods lost 3%
    or more."""
    returns = _periodic_returns(equity_curve)
    if not returns:
        return 0.0
    sorted_returns = sorted(returns)
    index = max(0, math.floor((1.0 - confidence) * len(sorted_returns)) - 1)
    index = min(index, len(sorted_returns) - 1)
    return max(-sorted_returns[index], 0.0)


def historical_cvar(equity_curve: list[tuple[date, float]], confidence: float = 0.95) -> float:
    """Historical CVaR (expected shortfall): the mean loss among periods
    at or beyond the VaR threshold — always >= VaR."""
    returns = _periodic_returns(equity_curve)
    if not returns:
        return 0.0
    sorted_returns = sorted(returns)
    cutoff = max(1, math.floor((1.0 - confidence) * len(sorted_returns)))
    tail = sorted_returns[:cutoff]
    if not tail:
        return 0.0
    return max(-_mean(tail), 0.0)


def trade_statistics(trades: list[TradeRecord]) -> dict[str, float]:
    if not trades:
        return dict(win_rate=0.0, average_winner=0.0, average_loser=0.0, profit_factor=0.0, expectancy=0.0)
    winners = [t.realistic_pnl for t in trades if t.realistic_pnl > 0]
    losers = [t.realistic_pnl for t in trades if t.realistic_pnl < 0]
    win_rate = len(winners) / len(trades)
    average_winner = _mean(winners) if winners else 0.0
    average_loser = abs(_mean(losers)) if losers else 0.0
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = _mean([t.realistic_pnl for t in trades])
    return dict(
        win_rate=win_rate, average_winner=average_winner, average_loser=average_loser,
        profit_factor=profit_factor, expectancy=expectancy,
    )


def capital_utilization(trades: list[TradeRecord], equity_curve: list[tuple[date, float]], initial_cash: float) -> float:
    """Time-weighted average fraction of `initial_cash` committed as
    collateral (`TradeRecord.capital_at_risk`) across every day the
    equity curve spans — 0 if the portfolio was never in a position,
    approaching 1 if it was consistently fully deployed."""
    if not equity_curve or initial_cash <= 0:
        return 0.0
    all_days = [d for d, _ in equity_curve]
    total_days = max((all_days[-1] - all_days[0]).days, 1)
    deployed_day_dollars = 0.0
    for trade in trades:
        held_days = max((trade.closed_at - trade.opened_at).days, 0)
        deployed_day_dollars += held_days * trade.capital_at_risk
    return deployed_day_dollars / (total_days * initial_cash)


def average_holding_period_days(trades: list[TradeRecord]) -> float:
    if not trades:
        return 0.0
    return _mean([float(t.holding_period_days) for t in trades])


def compute_metrics(
    equity_curve: list[tuple[date, float]],
    trades: list[TradeRecord],
    initial_cash: float,
    risk_free_annual_rate: float,
) -> PerformanceMetrics:
    stats = trade_statistics(trades)
    return PerformanceMetrics(
        cagr=cagr(equity_curve),
        annual_volatility=annual_volatility(equity_curve),
        sharpe=sharpe_ratio(equity_curve, risk_free_annual_rate),
        sortino=sortino_ratio(equity_curve, risk_free_annual_rate),
        max_drawdown=max_drawdown(equity_curve),
        calmar=calmar_ratio(equity_curve),
        win_rate=stats["win_rate"],
        average_winner=stats["average_winner"],
        average_loser=stats["average_loser"],
        profit_factor=stats["profit_factor"],
        expectancy=stats["expectancy"],
        var_95=historical_var(equity_curve),
        cvar_95=historical_cvar(equity_curve),
        worst_month=worst_month(equity_curve),
        worst_year=worst_year(equity_curve),
        longest_drawdown_days=longest_drawdown_days(equity_curve),
        capital_utilization=capital_utilization(trades, equity_curve, initial_cash),
        trade_count=len(trades),
        average_holding_period_days=average_holding_period_days(trades),
    )
