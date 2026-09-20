"""Risk Engine state and the RISK PANEL's aggregated view (Step 18).

Nothing here recomputes a risk *decision* on any trade — `src.risk.engine
.evaluate_trade_proposal` remains the sole authority on whether a
specific proposal is approved. This module answers the dashboard's own,
separate question — "what is this portfolio's current overall risk
posture, right now" — by composing `src.risk.drawdown`/`kill_switch`/
`concentration`/`correlation`'s own primitives exactly as the Risk
Engine itself does, entirely read-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from src.quant.correlations import flag_highly_correlated_pairs
from src.risk.drawdown import DrawdownZone, current_drawdown_pct, drawdown_zone
from src.risk.kill_switch import check_kill_switch
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, capital_deployed_pct, sector_exposure_pct, underlying_exposure_pct


class RiskEngineState(str, Enum):
    """The four states Step 18 names for the dashboard, mapped onto
    this codebase's own `DrawdownZone` vocabulary — `RISK_REDUCTION` ->
    `REDUCE_RISK` is a display-label difference only, never a second,
    independently-computed concept — plus HALT for the manual kill
    switch, which `DrawdownZone` alone doesn't cover."""

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    REDUCE_RISK = "REDUCE_RISK"
    HALT = "HALT"


_ZONE_TO_STATE: dict[DrawdownZone, RiskEngineState] = {
    DrawdownZone.NORMAL: RiskEngineState.NORMAL,
    DrawdownZone.WARNING: RiskEngineState.WARNING,
    DrawdownZone.RISK_REDUCTION: RiskEngineState.REDUCE_RISK,
    DrawdownZone.HALT: RiskEngineState.HALT,
}


@dataclass(frozen=True)
class ConcentrationEntry:
    label: str
    exposure_pct: float
    limit_pct: float

    @property
    def breached(self) -> bool:
        return self.exposure_pct > self.limit_pct


@dataclass(frozen=True)
class CorrelationCluster:
    tickers: tuple[str, str]
    correlation: float


@dataclass(frozen=True)
class RiskPanelView:
    state: RiskEngineState
    state_reason: str
    capital_utilization_pct: float
    cash_reserve_pct: float
    underlying_concentration: tuple[ConcentrationEntry, ...]
    sector_concentration: tuple[ConcentrationEntry, ...]
    correlation_clusters: tuple[CorrelationCluster, ...]
    correlation_tracked: bool
    current_drawdown_pct: float
    drawdown_zone: DrawdownZone


def compute_risk_engine_state(portfolio: Portfolio, limits: RiskLimitsConfig) -> tuple[RiskEngineState, str]:
    """The kill switch (manual halt, or drawdown past the halt
    threshold) always wins — exactly like the Risk Engine's own
    `_evaluate` step 1, "regardless of how the trade under review
    looks." Absent a halt, the state is simply the portfolio's current
    drawdown zone."""
    kill = check_kill_switch(portfolio, limits)
    if kill.halted:
        return RiskEngineState.HALT, kill.message or "portfolio halted"
    dd = current_drawdown_pct(portfolio)
    zone = drawdown_zone(dd, limits)
    state = _ZONE_TO_STATE[zone]
    reason = {
        RiskEngineState.NORMAL: f"drawdown {dd:.2%}, within normal range",
        RiskEngineState.WARNING: (
            f"drawdown {dd:.2%}, at or beyond the {limits.drawdown_warning_pct:.2%} warning threshold"
        ),
        RiskEngineState.REDUCE_RISK: (
            f"drawdown {dd:.2%}, at or beyond the {limits.drawdown_risk_reduction_pct:.2%} "
            "risk-reduction threshold — new position sizing is tightened"
        ),
    }[state]
    return state, reason


def underlying_concentration(portfolio: Portfolio, limits: RiskLimitsConfig) -> tuple[ConcentrationEntry, ...]:
    tickers = sorted({p.ticker for p in portfolio.positions})
    return tuple(
        ConcentrationEntry(
            label=ticker,
            exposure_pct=underlying_exposure_pct(portfolio, ticker),
            limit_pct=limits.max_underlying_exposure_pct,
        )
        for ticker in tickers
    )


def sector_concentration(portfolio: Portfolio, limits: RiskLimitsConfig) -> tuple[ConcentrationEntry, ...]:
    sectors = sorted({portfolio.sector_by_ticker.get(p.ticker, "UNKNOWN") for p in portfolio.positions})
    return tuple(
        ConcentrationEntry(
            label=sector,
            exposure_pct=sector_exposure_pct(portfolio, sector),
            limit_pct=limits.max_sector_exposure_pct,
        )
        for sector in sectors
    )


def correlation_clusters(portfolio: Portfolio, limits: RiskLimitsConfig) -> tuple[tuple[CorrelationCluster, ...], bool]:
    """Pairwise correlation across every currently-held pair of tickers
    with aligned price history — the dashboard's own read-only view,
    built from the identical `src.quant.correlations` primitive
    `src.risk.correlation` uses for its single-new-ticker check.

    Returns `(clusters, tracked)`. `tracked=False` (empty clusters, never
    a fabricated "no correlation risk" claim) whenever `price_history`
    isn't populated for at least two currently-held tickers — the same
    honestly-documented gap QF-001 (`SECURITY_AUDIT.md`) already named
    for the Risk Engine's own correlation check; this panel inherits it
    rather than papering over it with a silent default."""
    tickers = sorted({p.ticker for p in portfolio.positions})
    available = [t for t in tickers if t in portfolio.price_history]
    if len(available) < 2:
        return (), False
    series = {t: np.asarray(portfolio.price_history[t], dtype=float) for t in available}
    pairs = flag_highly_correlated_pairs(series, threshold=limits.high_correlation_threshold)
    clusters = tuple(CorrelationCluster(tickers=(p.symbol_a, p.symbol_b), correlation=p.correlation) for p in pairs)
    return clusters, True


def build_risk_panel(portfolio: Portfolio, limits: RiskLimitsConfig) -> RiskPanelView:
    state, reason = compute_risk_engine_state(portfolio, limits)
    dd = current_drawdown_pct(portfolio)
    zone = drawdown_zone(dd, limits)
    clusters, tracked = correlation_clusters(portfolio, limits)
    return RiskPanelView(
        state=state,
        state_reason=reason,
        capital_utilization_pct=capital_deployed_pct(portfolio),
        cash_reserve_pct=portfolio.cash / portfolio.nav,
        underlying_concentration=underlying_concentration(portfolio, limits),
        sector_concentration=sector_concentration(portfolio, limits),
        correlation_clusters=clusters,
        correlation_tracked=tracked,
        current_drawdown_pct=dd,
        drawdown_zone=zone,
    )
