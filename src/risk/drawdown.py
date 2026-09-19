"""Portfolio drawdown zone: how far current NAV sits below its
high-water mark, and what that implies for new risk.
"""
from __future__ import annotations

from enum import Enum

from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio


class DrawdownZone(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    RISK_REDUCTION = "risk_reduction"
    HALT = "halt"


def current_drawdown_pct(portfolio: Portfolio) -> float:
    """Fraction below the high-water mark, in [0, 1]. `Portfolio`'s own
    validator already guarantees `peak_equity >= nav`."""
    if portfolio.peak_equity <= 0:
        raise ValueError("peak_equity must be positive")
    return (portfolio.peak_equity - portfolio.nav) / portfolio.peak_equity


def drawdown_zone(drawdown_pct: float, limits: RiskLimitsConfig) -> DrawdownZone:
    if drawdown_pct >= limits.drawdown_halt_pct:
        return DrawdownZone.HALT
    if drawdown_pct >= limits.drawdown_risk_reduction_pct:
        return DrawdownZone.RISK_REDUCTION
    if drawdown_pct >= limits.drawdown_warning_pct:
        return DrawdownZone.WARNING
    return DrawdownZone.NORMAL


def sizing_multiplier_for_zone(zone: DrawdownZone, limits: RiskLimitsConfig) -> float:
    """How much the normal target-risk-per-trade budget should be
    scaled for new risk-adding trades in this zone. Only ever tightens
    (<= 1.0) — a drawdown zone can never loosen sizing beyond what
    `src.risk.trade_risk.size_trade` would otherwise allow."""
    if zone == DrawdownZone.RISK_REDUCTION:
        return limits.risk_reduction_sizing_multiplier
    return 1.0
