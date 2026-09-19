"""Risk-neutral probability calculations under the Black-Scholes
lognormal terminal-price distribution.
"""
from __future__ import annotations

from scipy.stats import norm

from src.quant.black_scholes import MIN_T, OptionRight, d2


def probability_above(spot: float, threshold: float, t: float, rate: float, sigma: float) -> float:
    """Risk-neutral probability that the underlying finishes above
    `threshold` at time t."""
    if t <= MIN_T:
        return 1.0 if spot > threshold else 0.0
    return float(norm.cdf(d2(spot, threshold, t, rate, sigma)))


def probability_below(spot: float, threshold: float, t: float, rate: float, sigma: float) -> float:
    return 1.0 - probability_above(spot, threshold, t, rate, sigma)


def probability_itm(
    spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight
) -> float:
    """Risk-neutral probability of finishing in-the-money at expiration."""
    if right == OptionRight.CALL:
        return probability_above(spot, strike, t, rate, sigma)
    return probability_below(spot, strike, t, rate, sigma)


def probability_of_profit(
    spot: float, breakeven: float, t: float, rate: float, sigma: float, *, bullish: bool = True
) -> float:
    """Probability the position is profitable at expiration, defined as
    finishing beyond its breakeven price. All three of this platform's
    initial strategies (cash-secured put, covered call, put credit
    spread) are net-credit structures profitable above their breakeven
    (`bullish=True`, the default); `bullish=False` is provided for a
    below-breakeven profit zone, should a bearish credit structure ever
    be added to the strategy menu."""
    if bullish:
        return probability_above(spot, breakeven, t, rate, sigma)
    return probability_below(spot, breakeven, t, rate, sigma)
