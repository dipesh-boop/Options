"""Portfolio concentration limits: how much of NAV can sit behind one
underlying, or one sector, after the proposed trade is added.
"""
from __future__ import annotations

from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct


class ConcentrationError(ValueError):
    pass


class UnderlyingConcentrationError(ConcentrationError):
    pass


class SectorConcentrationError(ConcentrationError):
    pass


def check_underlying_concentration(
    portfolio: Portfolio, ticker: str, additional_capital_at_risk: float, limits: RiskLimitsConfig
) -> None:
    pct = underlying_exposure_pct(portfolio, ticker, additional_capital_at_risk)
    if pct > limits.max_underlying_exposure_pct:
        raise UnderlyingConcentrationError(
            f"{ticker} exposure would be {pct:.2%} of NAV, exceeding the "
            f"{limits.max_underlying_exposure_pct:.2%} limit"
        )


def check_sector_concentration(
    portfolio: Portfolio, sector: str, additional_capital_at_risk: float, limits: RiskLimitsConfig
) -> None:
    pct = sector_exposure_pct(portfolio, sector, additional_capital_at_risk)
    if pct > limits.max_sector_exposure_pct:
        raise SectorConcentrationError(
            f"{sector} sector exposure would be {pct:.2%} of NAV, exceeding the "
            f"{limits.max_sector_exposure_pct:.2%} limit"
        )
