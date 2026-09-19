"""Black-Scholes-Merton European option pricing — the deterministic
pricing kernel every other module in src/quant builds on.

Pure functions only: no I/O, no randomness, no dependency on any other
part of this codebase. In particular this module (and every module in
src/quant) must never import from src.llm — Python Quant sits strictly
beneath the Multi-Agent Layer; an LLM consumes these calculations, it
never supplies or overrides them.

Pricing assumes European exercise and no dividends. The platform's
initial strategies (cash-secured put, covered call, put credit spread)
are American-style equity options in practice; early-exercise/dividend
risk is a separate, documented v1 approximation (ARCHITECTURE.md §6),
not folded into this pricing model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from scipy.stats import norm

MIN_T = 1e-8  # years; avoids division by zero at/after expiration
MIN_SIGMA = 1e-6  # avoids division by zero for a zero-vol input


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Leg:
    """One option leg of a (possibly multi-leg) position. `entry_price`
    is the premium per share at which this leg was actually transacted —
    pricing functions below never need it, but the P&L/stress functions
    in src.quant.monte_carlo do."""

    right: OptionRight
    strike: float
    side: Side
    entry_price: float
    quantity: int = 1


def d1(spot: float, strike: float, t: float, rate: float, sigma: float) -> float:
    t_eff = max(t, MIN_T)
    sigma_eff = max(sigma, MIN_SIGMA)
    return (math.log(spot / strike) + (rate + 0.5 * sigma_eff**2) * t_eff) / (sigma_eff * math.sqrt(t_eff))


def d2(spot: float, strike: float, t: float, rate: float, sigma: float) -> float:
    t_eff = max(t, MIN_T)
    sigma_eff = max(sigma, MIN_SIGMA)
    return d1(spot, strike, t, rate, sigma) - sigma_eff * math.sqrt(t_eff)


def intrinsic_value(spot: float, strike: float, right: OptionRight) -> float:
    if right == OptionRight.CALL:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def price(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> float:
    """Black-Scholes European option price. At/after expiration (t<=0)
    returns intrinsic value rather than dividing by zero."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if t <= 0:
        return intrinsic_value(spot, strike, right)

    _d1 = d1(spot, strike, t, rate, sigma)
    _d2 = d2(spot, strike, t, rate, sigma)
    disc_strike = strike * math.exp(-rate * t)

    if right == OptionRight.CALL:
        return spot * norm.cdf(_d1) - disc_strike * norm.cdf(_d2)
    return disc_strike * norm.cdf(-_d2) - spot * norm.cdf(-_d1)
