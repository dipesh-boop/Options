"""PORTFOLIO PERFORMANCE, RISK, TRADE STATISTICS, and BREAKDOWN
PERFORMANCE sections of `/weekly-review` (Step 16).

Every number here is computed by functions this codebase already built
and already tested — `src.backtest.metrics` (CAGR, Sharpe, Sortino, max
drawdown, trade statistics), `src.backtest.benchmark` (SPY/risk-free
comparison), and `src.research.performance_breakdown` (dimension
breakdowns) — applied to a real (paper or Fidelity-confirmed) trade
journal instead of a backtest run. `src.backtest.simulator.TradeRecord`
is reused unmodified as the trade-journal record shape: a closed
options trade's realistic/theoretical P&L fields describe backtest and
live trades identically, so a second, parallel "live trade record" type
would only be a second place for the same shape to drift.

There is no persisted equity ledger yet (a documented Phase 0 gap) —
`equity_curve` is supplied by the caller, exactly like
`src.workflows.morning_scan`'s `fetch_results` is a caller-supplied
already-fetched result rather than something this module fetches
itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.backtest.benchmark import BenchmarkComparison, compare_to_benchmarks
from src.backtest.metrics import (
    cagr,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    trade_statistics,
)
from src.backtest.simulator import TradeRecord
from src.data.historical import HistoricalBar
from src.research.performance_breakdown import (
    PerformanceBreakdownReport,
    ResearchTradeObservation,
    breakdown_by,
)

# The 8 dimensions Step 16 actually asks for -- a subset of the 12
# `src.research.performance_breakdown.AnalysisDimension` already
# defines; reused, not redefined.
WEEKLY_REVIEW_DIMENSIONS = ("strategy", "market_regime", "delta", "dte", "iv_percentile", "underlying", "sector", "holding_period")


def _nav_on_or_before(sorted_equity_curve: list[tuple[date, float]], target: date) -> float | None:
    """The last equity-curve point dated on or before `target` — used to
    anchor weekly/MTD/YTD returns to whatever NAV was actually recorded
    nearest that boundary, never an interpolated or invented one.
    Assumes `sorted_equity_curve` is already sorted ascending by date."""
    on_or_before = [nav for d, nav in sorted_equity_curve if d <= target]
    return on_or_before[-1] if on_or_before else None


@dataclass(frozen=True)
class PortfolioPerformanceSection:
    starting_nav: float
    ending_nav: float
    weekly_return: float | None
    mtd_return: float | None
    ytd_return: float | None
    since_inception_return: float
    annualized_return: float
    benchmark: BenchmarkComparison | None


def build_portfolio_performance(
    equity_curve: list[tuple[date, float]],
    *,
    week_start: date,
    month_start: date,
    year_start: date,
    risk_free_annual_rate: float,
    spy_bars: list[HistoricalBar] | None = None,
) -> PortfolioPerformanceSection:
    if len(equity_curve) < 2:
        raise ValueError("equity_curve must have at least two points to report performance")
    curve = sorted(equity_curve, key=lambda p: p[0])
    starting_nav, ending_nav = curve[0][1], curve[-1][1]
    end_date = curve[-1][0]

    week_start_nav = _nav_on_or_before(curve, week_start)
    month_start_nav = _nav_on_or_before(curve, month_start)
    year_start_nav = _nav_on_or_before(curve, year_start)

    benchmark = None
    if spy_bars is not None:
        benchmark = compare_to_benchmarks(
            strategy_realistic_return=ending_nav / starting_nav - 1.0,
            strategy_theoretical_return=ending_nav / starting_nav - 1.0,
            spy_bars=spy_bars,
            risk_free_annual_rate=risk_free_annual_rate,
            start=curve[0][0],
            end=end_date,
        )

    return PortfolioPerformanceSection(
        starting_nav=starting_nav,
        ending_nav=ending_nav,
        weekly_return=(ending_nav / week_start_nav - 1.0) if week_start_nav else None,
        mtd_return=(ending_nav / month_start_nav - 1.0) if month_start_nav else None,
        ytd_return=(ending_nav / year_start_nav - 1.0) if year_start_nav else None,
        since_inception_return=ending_nav / starting_nav - 1.0,
        annualized_return=cagr(curve),
        benchmark=benchmark,
    )


@dataclass(frozen=True)
class RiskSection:
    sharpe: float
    sortino: float
    max_drawdown: float
    current_drawdown_pct: float
    net_delta: float | None
    net_theta: float | None
    net_vega: float | None
    cash: float
    capital_deployed_pct: float
    sector_exposure: dict[str, float]
    underlying_concentration: dict[str, float]


def build_risk_section(
    equity_curve: list[tuple[date, float]],
    *,
    risk_free_annual_rate: float,
    current_drawdown_pct: float,
    cash: float,
    capital_deployed_pct: float,
    sector_exposure: dict[str, float],
    underlying_concentration: dict[str, float],
    net_delta: float | None = None,
    net_theta: float | None = None,
    net_vega: float | None = None,
) -> RiskSection:
    return RiskSection(
        sharpe=sharpe_ratio(equity_curve, risk_free_annual_rate),
        sortino=sortino_ratio(equity_curve, risk_free_annual_rate),
        max_drawdown=max_drawdown(equity_curve),
        current_drawdown_pct=current_drawdown_pct,
        net_delta=net_delta,
        net_theta=net_theta,
        net_vega=net_vega,
        cash=cash,
        capital_deployed_pct=capital_deployed_pct,
        sector_exposure=dict(sector_exposure),
        underlying_concentration=dict(underlying_concentration),
    )


def build_trade_statistics(trades: list[TradeRecord]) -> dict[str, float]:
    """`src.backtest.metrics.trade_statistics`, unmodified — win rate,
    average winner/loser, profit factor, expectancy."""
    return trade_statistics(trades)


def build_breakdowns(observations: list[ResearchTradeObservation]) -> tuple[PerformanceBreakdownReport, ...]:
    """The 8 dimensions Step 16 names, via
    `src.research.performance_breakdown.breakdown_by` -- never a second
    bucketing implementation."""
    return tuple(breakdown_by(observations, dim) for dim in WEEKLY_REVIEW_DIMENSIONS)  # type: ignore[arg-type]
