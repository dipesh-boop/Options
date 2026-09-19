"""The portfolio-wide kill switch: the one check that, if tripped,
overrides every other outcome and forces `RiskDecision.HALT` regardless
of how good any individual trade looks.

Two independent triggers, either sufficient on its own:
1. Automatic — NAV has drawn down past `limits.drawdown_halt_pct` from
   its high-water mark (src.risk.drawdown).
2. Manual — a human has set `Portfolio.halted=True` (an out-of-band
   emergency stop this module trusts verbatim; nothing in `src.risk`
   ever clears it automatically).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.risk.drawdown import DrawdownZone, current_drawdown_pct, drawdown_zone
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.risk.reason_codes import ReasonCode


@dataclass(frozen=True)
class KillSwitchResult:
    halted: bool
    reason_code: ReasonCode | None
    message: str | None


def check_kill_switch(portfolio: Portfolio, limits: RiskLimitsConfig) -> KillSwitchResult:
    if portfolio.halted:
        return KillSwitchResult(
            halted=True,
            reason_code=ReasonCode.HALT_MANUAL_KILL_SWITCH,
            message=portfolio.halt_reason or "portfolio halted manually",
        )

    dd = current_drawdown_pct(portfolio)
    if drawdown_zone(dd, limits) == DrawdownZone.HALT:
        return KillSwitchResult(
            halted=True,
            reason_code=ReasonCode.HALT_PORTFOLIO_DRAWDOWN,
            message=f"portfolio drawdown {dd:.2%} at or beyond halt threshold {limits.drawdown_halt_pct:.2%}",
        )

    return KillSwitchResult(halted=False, reason_code=None, message=None)
