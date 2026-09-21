"""Per-strategy performance tracking and attribution (Step 14B):
"Track performance individually. Do NOT judge only the combined
portfolio," plus the reporting system's own named questions ("which
strategies actually generated profit," etc.).

Extends, never replaces, Step 19's validation system and Step 14's
research breakdown machinery — every bucket-count/win-rate/P&L figure
below reuses `src.research.performance_breakdown.breakdown_by` directly
(the same `ResearchTradeObservation`/`TradeContext` shape that module
already defines) rather than a second grouping implementation. This
module adds the fields that breakdown alone doesn't compute
(return on capital, expectancy, profit factor, a per-trade Sharpe-like
ratio, a drawdown-contribution figure, average holding period, average
slippage) and then answers Step 14B's six attribution questions with
plain deterministic threshold rules over those numbers — never an LLM
judgment call, the same "Python computes, LLM narrates" rule every
other package in this codebase enforces.

Two figures below are honestly *approximations*, named and documented
as such rather than presented as the rigorous portfolio-level versions
this codebase already computes elsewhere:

- `sharpe`: a per-trade mean/stdev ratio over each trade's own
  `realistic_pnl / capital_at_risk`, **not** annualized and **not** the
  same equity-curve-based Sharpe `src.backtest.metrics.compute_metrics`
  computes for the whole portfolio (there is no per-strategy equity
  curve to compute that from). `None` below a minimum sample size
  (`meaningful_sample`-style gating, same discipline
  `src.workflows.rejected_trade_review` already applies) or when the
  per-trade return series has zero variance.
- `max_drawdown_contribution`: the largest peak-to-trough drop in a
  *pseudo* equity curve built by cumulatively summing this strategy's
  own trades' `realistic_pnl` in chronological order, starting at
  zero — a drawdown-*contribution* figure (Step 14B's own term), not a
  true portfolio-level percentage drawdown.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from src.research.performance_breakdown import ResearchTradeObservation, breakdown_by
from src.strategies.base import STRATEGY_FAMILIES, StrategyFamily, StrategyKind
from src.validation.counterfactual import StrategyAlternativeRecord

_RISK_APPROVAL_DECISIONS = ("approve", "resize")

DEFAULT_MIN_SAMPLE_FOR_SHARPE = 20  # same default src.validation.protocol's rejected_trade_min_sample_size already uses
_REGIME_DOMINANCE_THRESHOLD = 0.80  # same threshold src.validation.regime_analysis.RegimeCoverageSummary already uses
_TAIL_LOSS_MULTIPLE = 3.0  # worst single loss > this many times the average loss magnitude flags a fat tail
_EXCESSIVE_COST_FRACTION = 0.25  # slippage+commission exceeding this fraction of gross profit flags high trading costs

_HEDGE_FAMILIES = (StrategyFamily.PORTFOLIO_PROTECTION, StrategyFamily.TAIL_RISK_HEDGE)


def _is_hedge_strategy(strategy_value: str) -> bool:
    """True for a strategy whose own classification is portfolio
    protection / tail-risk hedging (protective put, protective collar)
    -- Step 19A's own rule that these must be judged by drawdown/tail-
    loss reduction, never standalone P&L alone, applied here as the
    basis for the "reduced losses" / "improved drawdown" attribution
    answers rather than a raw P&L sign check."""
    try:
        kind = StrategyKind(strategy_value)
    except ValueError:
        return False
    families = STRATEGY_FAMILIES.get(kind, ())
    return any(f in _HEDGE_FAMILIES for f in families)


@dataclass(frozen=True)
class StrategyPerformanceSummary:
    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    return_on_capital: float
    expectancy: float  # average realistic P&L per trade
    profit_factor: float | None  # gross profit / gross loss; None when there are zero losing trades
    sharpe: float | None  # see module docstring -- a per-trade approximation, None below the sample-size gate
    max_drawdown_contribution: float  # dollars, >= 0 -- see module docstring
    worst_trade_pnl: float  # <= 0; the single worst realistic P&L this strategy produced
    average_loss_magnitude: float | None  # mean |pnl| among losing trades only; None when there are no losing trades
    avg_holding_period_days: float
    avg_slippage: float  # dollars per trade, same (theoretical - realistic - commission) definition build_backtest_result uses
    avg_capital_deployed: float  # dollars per trade -- mean capital_at_risk, this strategy's own "capital utilization" figure
    performance_by_regime: dict[str, "RegimeSlice"]


@dataclass(frozen=True)
class RegimeSlice:
    regime: str
    trade_count: int
    win_rate: float
    total_pnl: float
    average_pnl: float


def _max_drawdown_contribution(chronological_pnls: list[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in chronological_pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _sharpe_per_trade(observations: list[ResearchTradeObservation], min_sample: int) -> float | None:
    if len(observations) < min_sample:
        return None
    returns = [
        o.trade.realistic_pnl / o.trade.capital_at_risk
        for o in observations
        if o.trade.capital_at_risk > 0
    ]
    if len(returns) < min_sample:
        return None
    stdev = statistics.pstdev(returns)
    if stdev == 0:
        return None
    return statistics.mean(returns) / stdev


def per_strategy_performance(
    observations: list[ResearchTradeObservation], *, min_sample_for_sharpe: int = DEFAULT_MIN_SAMPLE_FOR_SHARPE
) -> dict[str, StrategyPerformanceSummary]:
    """One `StrategyPerformanceSummary` per strategy actually present in
    `observations`, tracked individually -- never rolled into a single
    portfolio-wide figure."""
    by_strategy = breakdown_by(observations, "strategy")
    grouped: dict[str, list[ResearchTradeObservation]] = {}
    for obs in observations:
        grouped.setdefault(obs.trade.strategy.value, []).append(obs)

    summaries: dict[str, StrategyPerformanceSummary] = {}
    for bucket in by_strategy.buckets:
        group = grouped[bucket.bucket]
        pnls = [o.trade.realistic_pnl for o in group]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        capital_total = sum(o.trade.capital_at_risk for o in group)
        slippage = [o.trade.theoretical_pnl - o.trade.realistic_pnl - o.trade.commission_paid for o in group]
        chronological = [o.trade.realistic_pnl for o in sorted(group, key=lambda o: o.trade.closed_at)]

        regime_slices: dict[str, RegimeSlice] = {}
        for regime_bucket in breakdown_by(group, "market_regime").buckets:
            regime_slices[regime_bucket.bucket] = RegimeSlice(
                regime=regime_bucket.bucket, trade_count=regime_bucket.trade_count, win_rate=regime_bucket.win_rate,
                total_pnl=regime_bucket.total_pnl, average_pnl=regime_bucket.average_pnl,
            )

        summaries[bucket.bucket] = StrategyPerformanceSummary(
            strategy=bucket.bucket,
            trades=bucket.trade_count,
            wins=len(wins),
            losses=len(losses),
            win_rate=bucket.win_rate,
            net_pnl=bucket.total_pnl,
            return_on_capital=(bucket.total_pnl / capital_total) if capital_total > 0 else 0.0,
            expectancy=bucket.average_pnl,
            profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
            sharpe=_sharpe_per_trade(group, min_sample_for_sharpe),
            max_drawdown_contribution=_max_drawdown_contribution(chronological),
            worst_trade_pnl=min(pnls),
            average_loss_magnitude=(gross_loss / len(losses)) if losses else None,
            avg_holding_period_days=statistics.mean(o.trade.holding_period_days for o in group),
            avg_slippage=statistics.mean(slippage),
            avg_capital_deployed=capital_total / len(group),
            performance_by_regime=regime_slices,
        )
    return summaries


@dataclass(frozen=True)
class StrategyAttributionReport:
    """The reporting system's own six named questions, each answered by
    a plain deterministic rule over `summaries` (documented per field
    below) -- never an LLM judgment call. `summaries` is always carried
    alongside, so nothing here is the only place a number lives."""

    generated_profit: tuple[str, ...]
    reduced_losses: tuple[str, ...]
    consumed_capital_without_value: tuple[str, ...]
    regime_specific: tuple[str, ...]
    improved_drawdown: tuple[str, ...]
    increased_tail_risk: tuple[str, ...]
    generated_excessive_trading_costs: tuple[str, ...]
    summaries: dict[str, StrategyPerformanceSummary]


def answer_attribution_questions(summaries: dict[str, StrategyPerformanceSummary]) -> StrategyAttributionReport:
    generated_profit = tuple(sorted(s for s, summary in summaries.items() if summary.net_pnl > 0))

    # Hedges are judged by their own family classification, never
    # standalone P&L alone -- Step 19A's own rule, reused here.
    hedge_present = tuple(sorted(s for s in summaries if _is_hedge_strategy(s)))
    reduced_losses = hedge_present
    improved_drawdown = hedge_present

    consumed_capital_without_value = tuple(sorted(
        s for s, summary in summaries.items() if summary.net_pnl <= 0 and not _is_hedge_strategy(s)
    ))

    regime_specific = tuple(sorted(
        s for s, summary in summaries.items()
        if summary.performance_by_regime
        and max(r.trade_count for r in summary.performance_by_regime.values()) / summary.trades > _REGIME_DOMINANCE_THRESHOLD
    ))

    # The classic short-premium fat-tail shape: a single worst loss much
    # larger than this strategy's own typical (average) loss.
    increased_tail_risk = tuple(sorted(
        s for s, summary in summaries.items()
        if summary.average_loss_magnitude and abs(summary.worst_trade_pnl) > _TAIL_LOSS_MULTIPLE * summary.average_loss_magnitude
    ))

    generated_excessive_trading_costs = tuple(sorted(
        s for s, summary in summaries.items()
        if summary.expectancy > 0 and summary.avg_slippage > 0 and summary.avg_slippage / summary.expectancy > _EXCESSIVE_COST_FRACTION
    ))

    return StrategyAttributionReport(
        generated_profit=generated_profit,
        reduced_losses=reduced_losses,
        consumed_capital_without_value=consumed_capital_without_value,
        regime_specific=regime_specific,
        improved_drawdown=improved_drawdown,
        increased_tail_risk=increased_tail_risk,
        generated_excessive_trading_costs=generated_excessive_trading_costs,
        summaries=summaries,
    )


@dataclass(frozen=True)
class StrategyFunnelCounts:
    """The decision-time funnel this step's re-ask names explicitly,
    distinct from `StrategyPerformanceSummary`'s completed-trade stats:
    how often a strategy was even generated as a candidate, how often it
    won the internal comparison and was put forward, how often the Risk
    Engine actually rejected it, and how often it became a real
    position. Sourced from `src.validation.counterfactual
    .StrategyAlternativeRecord` (already captured at decision time for
    every serious candidate, selected or not) rather than a second
    decision-tracking mechanism."""

    strategy: str
    opportunities_considered: int  # distinct opportunities where this strategy was generated as a candidate at all
    trades_proposed: int  # won the internal risk-adjusted comparison and was put forward as the opportunity's trade
    trades_rejected: int  # the Risk Engine's own verdict on this candidate was not an approval/resize
    trades_entered: int  # proposed AND risk-approved -- became a real position


def funnel_counts_by_strategy(records: list[StrategyAlternativeRecord]) -> dict[str, StrategyFunnelCounts]:
    grouped: dict[str, list[StrategyAlternativeRecord]] = {}
    for r in records:
        grouped.setdefault(r.evaluation.strategy_kind.value, []).append(r)

    counts: dict[str, StrategyFunnelCounts] = {}
    for strategy, group in grouped.items():
        proposed = [r for r in group if r.was_selected]
        rejected = [r for r in group if r.risk_decision.lower() not in _RISK_APPROVAL_DECISIONS]
        entered = [r for r in proposed if r.risk_decision.lower() in _RISK_APPROVAL_DECISIONS]
        counts[strategy] = StrategyFunnelCounts(
            strategy=strategy,
            opportunities_considered=len({r.opportunity_id for r in group}),
            trades_proposed=len(proposed),
            trades_rejected=len(rejected),
            trades_entered=len(entered),
        )
    return counts
