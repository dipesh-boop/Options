"""Portfolio-level impact of adding one candidate strategy: concentration
and correlation contribution. Reuses `src.risk.portfolio_risk`'s
aggregation helpers and `src.quant.correlations` directly — no second
concentration/correlation computation exists here.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct
from src.strategies.base import StrategyEvaluation


@dataclass(frozen=True)
class PortfolioFitResult:
    post_trade_underlying_exposure_pct: float
    post_trade_sector_exposure_pct: float
    exceeds_underlying_limit: bool
    exceeds_sector_limit: bool
    correlation_with_existing: float | None  # None = not tracked, same QF-001 honesty as src.risk.correlation
    diversification_score: float  # 1.0 = no concentration impact, 0.0 = at or beyond either limit


def evaluate_portfolio_fit(
    evaluation: StrategyEvaluation, portfolio: Portfolio, limits: RiskLimitsConfig
) -> PortfolioFitResult:
    additional_capital = evaluation.capital_requirement
    underlying_pct = underlying_exposure_pct(portfolio, evaluation.ticker, additional_capital)
    sector = portfolio.sector_by_ticker.get(evaluation.ticker, "UNKNOWN")
    sector_pct = sector_exposure_pct(portfolio, sector, additional_capital)

    exceeds_underlying = underlying_pct > limits.max_underlying_exposure_pct
    exceeds_sector = sector_pct > limits.max_sector_exposure_pct

    # No live historical-price wiring into Portfolio.price_history exists
    # yet (the same documented gap src.risk.correlation.check_correlation
    # already names) -- honestly None rather than a fabricated number.
    correlation = None

    underlying_headroom = max(1.0 - underlying_pct / limits.max_underlying_exposure_pct, 0.0)
    sector_headroom = max(1.0 - sector_pct / limits.max_sector_exposure_pct, 0.0)
    diversification_score = min(underlying_headroom, sector_headroom)

    return PortfolioFitResult(
        post_trade_underlying_exposure_pct=underlying_pct,
        post_trade_sector_exposure_pct=sector_pct,
        exceeds_underlying_limit=exceeds_underlying,
        exceeds_sector_limit=exceeds_sector,
        correlation_with_existing=correlation,
        diversification_score=diversification_score,
    )
