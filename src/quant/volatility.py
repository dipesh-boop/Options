"""Implied volatility: solves for the sigma that reprices a European
option to a given observed market price, via Newton-Raphson with a
bisection fallback for robustness when vega is too small (deep ITM/OTM,
near expiration) for Newton-Raphson to converge cleanly.
"""
from __future__ import annotations

import math

from src.quant.black_scholes import MIN_SIGMA, MIN_T, OptionRight, price
from src.quant.greeks import vega as vega_per_point

MAX_SIGMA = 5.0  # 500% annualized vol — a generous but finite search bound


def _european_lower_bound(spot: float, strike: float, t: float, rate: float, right: OptionRight) -> float:
    """The correct no-arbitrage floor for a *European* option price is
    the discounted intrinsic value, not the naive (American-style)
    intrinsic value. A deep-ITM European put can legitimately price
    below `strike - spot` when the discounting effect on the strike
    dominates — that's not a violation of any bound, since early
    exercise isn't available to compare against."""
    disc_strike = strike * math.exp(-rate * t)
    if right == OptionRight.CALL:
        return max(spot - disc_strike, 0.0)
    return max(disc_strike - spot, 0.0)


def implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    t: float,
    rate: float,
    right: OptionRight,
    *,
    initial_guess: float = 0.4,
    max_iterations: int = 50,
    tolerance: float = 1e-6,
) -> float | None:
    """Returns the implied volatility, or None if `market_price` is
    below the European no-arbitrage floor or at/after expiration (no
    volatility can be implied from an expired option's price)."""
    if t <= MIN_T:
        return None

    floor_value = _european_lower_bound(spot, strike, t, rate, right)
    if market_price < floor_value - 1e-6:
        return None

    sigma = initial_guess
    for _ in range(max_iterations):
        theo = price(spot, strike, t, rate, sigma, right)
        diff = theo - market_price
        if abs(diff) < tolerance:
            return max(sigma, MIN_SIGMA)
        v = vega_per_point(spot, strike, t, rate, sigma) * 100.0  # back to per-unit-sigma
        if v < 1e-8:
            break
        sigma -= diff / v
        if sigma <= 0 or sigma > MAX_SIGMA:
            break

    return _bisect_implied_volatility(market_price, spot, strike, t, rate, right, tolerance)


def _bisect_implied_volatility(
    market_price: float, spot: float, strike: float, t: float, rate: float, right: OptionRight, tolerance: float
) -> float | None:
    lo, hi = MIN_SIGMA, MAX_SIGMA
    price_lo = price(spot, strike, t, rate, lo, right)
    price_hi = price(spot, strike, t, rate, hi, right)
    if not (price_lo <= market_price <= price_hi):
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        theo = price(spot, strike, t, rate, mid, right)
        if abs(theo - market_price) < tolerance:
            return mid
        if theo < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
