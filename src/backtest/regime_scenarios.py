"""Reusable, deterministic market-regime scenario generation for testing
every approved strategy across the 8 regimes Step 19A names: BULL,
BEAR, SIDEWAYS, HIGH_VOLATILITY, LOW_VOLATILITY, VOLATILITY_EXPANSION,
VOLATILITY_CONTRACTION, MARKET_CRASH.

"Do not require one strategy to always win in any regime" is enforced
by what this module deliberately does NOT do: it never asserts a P&L
sign or win/loss outcome for any regime. It only generates a
regime-shaped underlying price path (risk-neutral GBM with a
regime-specific drift/vol, seeded for reproducibility) and an
IV-per-day path — the one thing a strategy actually must hold in every
regime is its own DEFINED-RISK BOUND (a put credit spread must never
lose more than its own `maximum_loss`, in a crash or otherwise), which
`tests/unit/backtest/test_regime_scenarios.py` checks directly against
`src.quant.monte_carlo.payoff_profile`'s already-proven-exact bound,
not against this module's simulated paths.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np


class RegimeScenario(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_CONTRACTION = "volatility_contraction"
    MARKET_CRASH = "market_crash"


@dataclass(frozen=True)
class RegimeParameters:
    """Annualized drift and starting/ending IV for the synthetic path.
    These are DELIBERATELY simple, documented, illustrative parameters
    for generating a plausible-shaped test scenario — not a calibrated
    market model and not a claim about real future returns (the same
    "provisional, not the real thing" status `src.workflows
    .candidate_generation`'s screener already carries)."""

    annual_drift: float
    start_iv: float
    end_iv: float
    crash_day: int | None = None  # if set, a single large negative jump occurs on this day index
    crash_magnitude: float = 0.0  # fractional drop applied on crash_day


_REGIME_PARAMETERS: dict[RegimeScenario, RegimeParameters] = {
    RegimeScenario.BULL: RegimeParameters(annual_drift=0.25, start_iv=0.18, end_iv=0.16),
    RegimeScenario.BEAR: RegimeParameters(annual_drift=-0.25, start_iv=0.22, end_iv=0.28),
    RegimeScenario.SIDEWAYS: RegimeParameters(annual_drift=0.0, start_iv=0.16, end_iv=0.16),
    RegimeScenario.HIGH_VOLATILITY: RegimeParameters(annual_drift=0.0, start_iv=0.45, end_iv=0.45),
    RegimeScenario.LOW_VOLATILITY: RegimeParameters(annual_drift=0.05, start_iv=0.10, end_iv=0.10),
    RegimeScenario.VOLATILITY_EXPANSION: RegimeParameters(annual_drift=0.0, start_iv=0.15, end_iv=0.40),
    RegimeScenario.VOLATILITY_CONTRACTION: RegimeParameters(annual_drift=0.05, start_iv=0.40, end_iv=0.15),
    RegimeScenario.MARKET_CRASH: RegimeParameters(
        annual_drift=-0.10, start_iv=0.20, end_iv=0.55, crash_day=10, crash_magnitude=0.20,
    ),
}


@dataclass(frozen=True)
class RegimePath:
    regime: RegimeScenario
    prices: tuple[float, ...]  # daily closes, prices[0] == start_price
    implied_vols: tuple[float, ...]  # one per day, same length as prices


def generate_regime_path(
    regime: RegimeScenario, *, start_price: float, num_days: int, seed: int, daily_vol_override: float | None = None
) -> RegimePath:
    """A deterministic (seeded) daily price path shaped like the named
    regime, using ordinary risk-neutral-flavored GBM for the routine
    day-to-day moves, plus an explicit single-day jump for
    MARKET_CRASH (a GBM path alone rarely produces a realistic crash-
    sized single-day move at a reasonable annualized vol input)."""
    if start_price <= 0:
        raise ValueError("start_price must be positive")
    if num_days < 2:
        raise ValueError("num_days must be at least 2")
    params = _REGIME_PARAMETERS[regime]
    rng = np.random.default_rng(seed)
    dt = 1.0 / 252.0

    ivs = np.linspace(params.start_iv, params.end_iv, num_days)
    daily_vol = daily_vol_override if daily_vol_override is not None else float(np.mean(ivs))

    prices = [start_price]
    for day in range(1, num_days):
        z = rng.standard_normal()
        drift_term = (params.annual_drift - 0.5 * daily_vol**2) * dt
        diffusion_term = daily_vol * math.sqrt(dt) * z
        next_price = prices[-1] * math.exp(drift_term + diffusion_term)
        if params.crash_day is not None and day == params.crash_day:
            next_price *= 1.0 - params.crash_magnitude
        prices.append(max(next_price, 0.01))

    return RegimePath(regime=regime, prices=tuple(prices), implied_vols=tuple(float(v) for v in ivs))


ALL_REGIME_SCENARIOS: tuple[RegimeScenario, ...] = tuple(RegimeScenario)
