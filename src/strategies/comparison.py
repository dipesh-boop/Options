"""The Strategy Comparison Engine: a full metrics table across every
candidate. The risk-adjusted ranking rule itself lives in
`src.strategies.ranking` (Step 14B split this into its own module) — this
module only builds `ComparisonRow`, so a table row and the ranking
formula it feeds can be read, tested, and changed independently. Neither
collapses the full comparison table into just the winning score for
reporting — the table (`ComparisonRow`, one per candidate) is always
preserved alongside a ranking, the same "never collapsed" discipline
`src.validation.scorecard` already applies to the 90-day report.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategies.base import StrategyEvaluation, StrategyKind
from src.strategies.portfolio_fit import PortfolioFitResult
from src.strategies.ranking import rank_candidates, risk_adjusted_score

__all__ = ["ComparisonRow", "build_comparison_row", "build_comparison_table", "rank_candidates", "risk_adjusted_score"]


@dataclass(frozen=True)
class ComparisonRow:
    strategy_kind: StrategyKind
    expected_value: float
    expected_return_on_capital: float
    maximum_profit: float
    maximum_loss: float
    probability_of_profit: float
    probability_of_max_loss: float | None
    expected_shortfall: float | None
    breakeven_distance_pct: float  # nearest breakeven's distance from the position's own current mark, as a fraction
    delta: float
    gamma: float
    theta: float
    vega: float
    capital_requirement: float
    buying_power_requirement: float
    liquidity_score: float
    estimated_slippage: float
    tail_risk: float  # |expected_shortfall| / maximum_loss, >1.0 flags a fatter tail than the defined max loss alone suggests
    assignment_risk: str
    event_risk: str
    portfolio_correlation: float | None
    concentration_impact_pct: float
    execution_complexity: str
    risk_adjusted_score: float


def _nearest_breakeven_distance_pct(evaluation: StrategyEvaluation, spot: float) -> float:
    if not evaluation.breakeven_points or spot <= 0:
        return 0.0
    nearest = min(evaluation.breakeven_points, key=lambda b: abs(b - spot))
    return abs(nearest - spot) / spot


def build_comparison_row(evaluation: StrategyEvaluation, fit: PortfolioFitResult, *, spot: float) -> ComparisonRow:
    pm = evaluation.probability_metrics
    tail_risk = (
        abs(pm.expected_shortfall) / evaluation.maximum_loss
        if pm.expected_shortfall is not None and evaluation.maximum_loss > 0
        else 0.0
    )
    return ComparisonRow(
        strategy_kind=evaluation.strategy_kind,
        expected_value=evaluation.expected_value,
        expected_return_on_capital=evaluation.return_on_capital,
        maximum_profit=evaluation.maximum_profit,
        maximum_loss=evaluation.maximum_loss,
        probability_of_profit=pm.probability_of_profit,
        probability_of_max_loss=pm.probability_of_max_loss,
        expected_shortfall=pm.expected_shortfall,
        breakeven_distance_pct=_nearest_breakeven_distance_pct(evaluation, spot),
        delta=evaluation.delta, gamma=evaluation.gamma, theta=evaluation.theta, vega=evaluation.vega,
        capital_requirement=evaluation.capital_requirement,
        buying_power_requirement=evaluation.buying_power_requirement,
        liquidity_score=evaluation.liquidity_score,
        estimated_slippage=evaluation.estimated_slippage,
        tail_risk=tail_risk,
        assignment_risk=evaluation.assignment_risk,
        event_risk=evaluation.event_risk,
        portfolio_correlation=fit.correlation_with_existing,
        concentration_impact_pct=fit.post_trade_underlying_exposure_pct,
        execution_complexity=evaluation.execution_complexity,
        risk_adjusted_score=risk_adjusted_score(evaluation),
    )


def build_comparison_table(
    evaluations: list[StrategyEvaluation], fits: dict[StrategyKind, PortfolioFitResult], *, spot: float
) -> list[ComparisonRow]:
    return [build_comparison_row(ev, fits[ev.strategy_kind], spot=spot) for ev in evaluations]
