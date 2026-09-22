"""Deterministic portfolio exposure snapshot (Step 22.4 Part 15).

Reuses `src.risk.portfolio_risk`'s existing per-ticker/per-sector
exposure helpers (`underlying_exposure_pct`/`sector_exposure_pct`) --
this module adds no new concentration *math* for the dimensions those
already cover, only the aggregation across every ticker/sector actually
held so a caller gets one full snapshot instead of having to already
know which tickers/sectors to ask about. `config/risk_limits.yaml`'s
existing limits (1%/2% per-trade, 10% underlying, 25% sector, 20%
minimum cash, 60%/70% deployed, 8%/10%/15% drawdown) remain the sole
authoritative thresholds -- this module reports *current* exposure, it
enforces nothing and loosens nothing.

**Assignment risk is read here, never computed here** (CLAUDE.md: "Do
not duplicate lifecycle logic — call the V1.3 Lifecycle Engine"):
`build_exposure_snapshot`'s `assignment_risk_position_ids` parameter is
supplied by the caller from `src.lifecycle.triggers.check_assignment_risk`
output, not re-derived from strike/spot comparisons in this module.
Likewise `portfolio_delta`/`portfolio_vega` (for directional/volatility
exposure) are supplied from `src.portfolio.revaluation.revalue_portfolio`
rather than recomputed -- one authoritative Greeks computation per
cycle, read by every module that needs it, never two independent ones
that could silently disagree.

Correlated-exposure reporting reuses `src.risk.correlation`'s own
documented gap: when `Portfolio.price_history` doesn't cover a ticker
pair, that pair is simply left out of `correlated_pairs`, not treated as
a fail-closed unknown-risk condition -- the same choice
`src.risk.correlation.check_correlation` already makes for the identical
reason (no live historical-data wiring into the Risk Engine yet).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from src.quant.correlations import CorrelatedPair, flag_highly_correlated_pairs
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct
from src.wheel.models import WheelPosition
from src.wheel.state import TERMINAL_STATES, WheelState

# A halted (not yet exited) Wheel still holds real cash/share commitment
# -- only a terminal state (src.wheel.state.TERMINAL_STATES: expired/
# closed/complete/exited/rejected) means the capital is actually free
# again, so this reuses that existing set rather than defining a second,
# possibly-drifting notion of "still committed."
_WHEEL_ACTIVE_STATES = frozenset(WheelState) - TERMINAL_STATES


class PortfolioExposureSnapshot(BaseModel):
    """One cycle's full Part 15 exposure picture. Every `*_pct` field is
    a fraction of `Portfolio.nav` (0.30 = 30%), matching
    `src.risk.portfolio_risk`'s own convention, so it compares directly
    against `RiskLimitsConfig`'s percentage fields without a caller
    having to convert units."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime

    underlying_exposure_pct: dict[str, float]
    sector_exposure_pct: dict[str, float]
    strategy_exposure_pct: dict[str, float]
    expiration_concentration_pct: dict[date, float]

    directional_exposure: Literal["net_long", "net_short", "neutral", "unknown"]
    portfolio_delta: float | None
    volatility_exposure: Literal["net_long_vol", "net_short_vol", "neutral", "unknown"]
    portfolio_vega: float | None

    short_option_capital_pct: float

    assignment_risk_position_ids: tuple[str, ...]

    wheel_cash_commitment_pct: float
    owned_share_exposure_pct: float
    owned_share_prices_missing: tuple[str, ...]  # tickers held whose current price wasn't supplied
    covered_call_encumbered_shares: dict[str, int]

    correlated_pairs: tuple[CorrelatedPair, ...]


def _directional_label(delta: float | None, *, neutral_band: float = 1.0) -> Literal["net_long", "net_short", "neutral", "unknown"]:
    if delta is None:
        return "unknown"
    if abs(delta) < neutral_band:
        return "neutral"
    return "net_long" if delta > 0 else "net_short"


def _volatility_label(vega: float | None, *, neutral_band: float = 1.0) -> Literal["net_long_vol", "net_short_vol", "neutral", "unknown"]:
    if vega is None:
        return "unknown"
    if abs(vega) < neutral_band:
        return "neutral"
    return "net_long_vol" if vega > 0 else "net_short_vol"


def build_exposure_snapshot(
    portfolio: Portfolio,
    *,
    as_of: datetime,
    limits: RiskLimitsConfig,  # noqa: ARG001 -- accepted for signature symmetry with concentration.py's callers; thresholds are applied by the caller, not this snapshot
    portfolio_delta: float | None = None,
    portfolio_vega: float | None = None,
    assignment_risk_position_ids: tuple[str, ...] = (),
    wheel_positions: tuple[WheelPosition, ...] = (),
    current_underlying_prices: dict[str, float] | None = None,
) -> PortfolioExposureSnapshot:
    """Builds the full Part 15 snapshot from a `Portfolio` plus whatever
    the caller has already computed elsewhere this cycle
    (Greeks from `src.portfolio.revaluation`, assignment risk from
    `src.lifecycle.triggers`, Wheel state from `src.wheel.persistence`).
    Never fetches anything itself -- every input is a plain, already-
    validated argument."""
    current_underlying_prices = current_underlying_prices or {}

    tickers = {p.ticker for p in portfolio.positions}
    sectors = {portfolio.sector_by_ticker.get(t, "UNKNOWN") for t in tickers}
    underlying_pct = {t: underlying_exposure_pct(portfolio, t) for t in sorted(tickers)}
    sector_pct = {s: sector_exposure_pct(portfolio, s) for s in sorted(sectors)}

    strategy_capital: dict[str, float] = {}
    expiration_capital: dict[date, float] = {}
    short_capital = 0.0
    for p in portfolio.positions:
        strategy_capital[p.strategy.value] = strategy_capital.get(p.strategy.value, 0.0) + p.capital_at_risk
        expiration_capital[p.expiration] = expiration_capital.get(p.expiration, 0.0) + p.capital_at_risk
        if any(leg.side == "sell" for leg in p.legs):
            short_capital += p.capital_at_risk
    strategy_pct = {k: v / portfolio.nav for k, v in strategy_capital.items()}
    expiration_pct = {k: v / portfolio.nav for k, v in expiration_capital.items()}

    wheel_cash_committed = sum(
        w.accounting.capital_committed for w in wheel_positions if w.state in _WHEEL_ACTIVE_STATES
    )

    owned_share_value = 0.0
    missing_prices: list[str] = []
    for ticker, holding in portfolio.underlying_holdings.items():
        price = current_underlying_prices.get(ticker)
        if price is None:
            missing_prices.append(ticker)
            owned_share_value += holding.shares * holding.cost_basis  # last-known-basis fallback, flagged above
            continue
        owned_share_value += holding.shares * price

    covered_call_encumbered = {
        w.ticker: w.open_cc_cycle.contracts * 100
        for w in wheel_positions
        if w.open_cc_cycle is not None
    }

    correlated: tuple[CorrelatedPair, ...] = ()
    eligible = {t for t in tickers if t in portfolio.price_history}
    if len(eligible) >= 2:
        lengths = {len(portfolio.price_history[t]) for t in eligible}
        if len(lengths) == 1:
            series = {t: np.asarray(portfolio.price_history[t], dtype=float) for t in eligible}
            correlated = tuple(flag_highly_correlated_pairs(series, threshold=limits.high_correlation_threshold))

    return PortfolioExposureSnapshot(
        as_of=as_of,
        underlying_exposure_pct=underlying_pct,
        sector_exposure_pct=sector_pct,
        strategy_exposure_pct=strategy_pct,
        expiration_concentration_pct=expiration_pct,
        directional_exposure=_directional_label(portfolio_delta),
        portfolio_delta=portfolio_delta,
        volatility_exposure=_volatility_label(portfolio_vega),
        portfolio_vega=portfolio_vega,
        short_option_capital_pct=short_capital / portfolio.nav,
        assignment_risk_position_ids=assignment_risk_position_ids,
        wheel_cash_commitment_pct=wheel_cash_committed / portfolio.nav,
        owned_share_exposure_pct=owned_share_value / portfolio.nav,
        owned_share_prices_missing=tuple(sorted(missing_prices)),
        covered_call_encumbered_shares=covered_call_encumbered,
        correlated_pairs=correlated,
    )
