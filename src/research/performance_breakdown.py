"""Performance breakdown by dimension — the "ANALYZE" section of Step
14: strategy, delta, DTE, IV percentile, market regime, underlying,
sector, entry day, entry time, holding period, profit target, management
DTE. Every number here is plain Python arithmetic over already-computed
`src.backtest.simulator.TradeRecord`s (never a number the LLM invents),
consumed as read-only reference data by `src.llm.strategy_research`.

**Multi-strategy attribution reporting addition**: a 13th dimension,
`volatility_regime`, was added on top of Step 14's original 12 —
deliberately distinct from `iv_percentile` (a numeric bucket like
"50-75") and from `src.validation.regime_analysis.ValidationRegime`
(direction + volatility combined, for the 90-day validation run's own
regime coverage question). `volatility_regime` is a plain,
caller-supplied qualitative label (e.g. "low_vol"/"normal"/
"elevated_vol"/"crisis", the same vocabulary
`MarketRegimeAssessment.regime` already uses) — a third, deliberately
distinct taxonomy for a fourth purpose (per-strategy performance
attribution), not a redefinition of either existing one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.backtest.simulator import TradeRecord

AnalysisDimension = Literal[
    "strategy",
    "delta",
    "dte",
    "iv_percentile",
    "market_regime",
    "volatility_regime",
    "underlying",
    "sector",
    "entry_day",
    "entry_time",
    "holding_period",
    "profit_target",
    "management_dte",
]

ALL_ANALYSIS_DIMENSIONS: tuple[str, ...] = tuple(AnalysisDimension.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class TradeContext:
    """Analysis-only metadata one `TradeRecord` doesn't itself carry —
    supplied by whoever ran the backtest (the same information an
    `EntrySignal`/quote lookup already had at entry time), never
    invented here. `entry_time_of_day` is deliberately optional: this
    platform's historical option-chain data (`HistoricalOptionQuote`,
    Step 13) is end-of-day, so a real "entry time" dimension has nothing
    to be computed from yet — `None` buckets honestly as "unspecified"
    rather than fabricating a time."""

    entry_delta: float  # signed, e.g. -0.20 for a 20-delta short put
    entry_dte: int
    iv_percentile: float  # 0-100
    market_regime: str
    sector: str
    profit_target_pct: float
    management_dte: int
    entry_time_of_day: str | None = None
    volatility_regime: str | None = None  # e.g. "low_vol"/"normal"/"elevated_vol"/"crisis" -- see module docstring


@dataclass(frozen=True)
class ResearchTradeObservation:
    """One closed trade plus the context needed to bucket it along every
    analysis dimension."""

    trade: TradeRecord
    context: TradeContext


@dataclass(frozen=True)
class BucketStats:
    bucket: str
    trade_count: int
    win_rate: float
    total_pnl: float
    average_pnl: float
    best_pnl: float
    worst_pnl: float


@dataclass(frozen=True)
class PerformanceBreakdownReport:
    dimension: AnalysisDimension
    buckets: tuple[BucketStats, ...]
    total_trades: int


def _delta_bucket(delta: float) -> str:
    magnitude = abs(delta) * 100
    if magnitude < 10:
        return "0-10"
    if magnitude < 15:
        return "10-15"
    if magnitude < 20:
        return "15-20"
    if magnitude < 25:
        return "20-25"
    if magnitude < 30:
        return "25-30"
    if magnitude < 40:
        return "30-40"
    return "40+"


def _dte_bucket(dte: int) -> str:
    if dte < 7:
        return "0-7"
    if dte < 14:
        return "7-14"
    if dte < 21:
        return "14-21"
    if dte < 30:
        return "21-30"
    if dte < 45:
        return "30-45"
    return "45+"


def _iv_percentile_bucket(pct: float) -> str:
    if pct < 25:
        return "0-25"
    if pct < 50:
        return "25-50"
    if pct < 75:
        return "50-75"
    return "75-100"


def _holding_period_bucket(days: int) -> str:
    if days < 7:
        return "0-7d"
    if days < 14:
        return "7-14d"
    if days < 30:
        return "14-30d"
    if days < 45:
        return "30-45d"
    return "45d+"


def _group_key(dimension: AnalysisDimension, obs: ResearchTradeObservation) -> str:
    trade, ctx = obs.trade, obs.context
    if dimension == "strategy":
        return trade.strategy.value
    if dimension == "delta":
        return _delta_bucket(ctx.entry_delta)
    if dimension == "dte":
        return _dte_bucket(ctx.entry_dte)
    if dimension == "iv_percentile":
        return _iv_percentile_bucket(ctx.iv_percentile)
    if dimension == "market_regime":
        return ctx.market_regime
    if dimension == "volatility_regime":
        return ctx.volatility_regime or "unspecified"
    if dimension == "underlying":
        return trade.ticker
    if dimension == "sector":
        return ctx.sector
    if dimension == "entry_day":
        return trade.opened_at.strftime("%A")
    if dimension == "entry_time":
        return ctx.entry_time_of_day or "unspecified"
    if dimension == "holding_period":
        return _holding_period_bucket(trade.holding_period_days)
    if dimension == "profit_target":
        return f"{ctx.profit_target_pct:.0%}"
    if dimension == "management_dte":
        return str(ctx.management_dte)
    raise ValueError(f"unknown analysis dimension: {dimension!r}")


def breakdown_by(observations: list[ResearchTradeObservation], dimension: AnalysisDimension) -> PerformanceBreakdownReport:
    """Groups `observations` by `dimension` and computes per-bucket
    trade count, win rate, total/average/best/worst realistic P&L —
    realistic, never theoretical, since this package researches what
    actually would have happened to execute, not a frictionless
    fantasy."""
    if dimension not in ALL_ANALYSIS_DIMENSIONS:
        raise ValueError(f"unknown analysis dimension: {dimension!r}")

    buckets: dict[str, list[ResearchTradeObservation]] = {}
    for obs in observations:
        key = _group_key(dimension, obs)
        buckets.setdefault(key, []).append(obs)

    stats: list[BucketStats] = []
    for bucket, group in sorted(buckets.items()):
        pnls = [o.trade.realistic_pnl for o in group]
        winners = [p for p in pnls if p > 0]
        stats.append(
            BucketStats(
                bucket=bucket,
                trade_count=len(group),
                win_rate=len(winners) / len(group),
                total_pnl=sum(pnls),
                average_pnl=sum(pnls) / len(group),
                best_pnl=max(pnls),
                worst_pnl=min(pnls),
            )
        )
    return PerformanceBreakdownReport(dimension=dimension, buckets=tuple(stats), total_trades=len(observations))


def breakdown_all_dimensions(observations: list[ResearchTradeObservation]) -> tuple[PerformanceBreakdownReport, ...]:
    """Every dimension the spec names, computed together — the
    convenience entry point `src.llm.strategy_research` actually calls."""
    return tuple(breakdown_by(observations, dim) for dim in ALL_ANALYSIS_DIMENSIONS)  # type: ignore[arg-type]
