"""Option Greeks — delta, gamma, theta, vega, rho — each its own
independent closed-form function, not a finite-difference approximation
of price. (Tests cross-check these against finite differences of
black_scholes.price as an independent verification path; production
code never derives greeks that way, for speed and numerical stability.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

from src.quant.black_scholes import MIN_SIGMA, MIN_T, Leg, OptionRight, Side, d1, d2


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point (e.g. IV moving from 20% to 21%)
    rho: float  # per 1% rate move


def delta(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> float:
    _d1 = d1(spot, strike, t, rate, sigma)
    if right == OptionRight.CALL:
        return float(norm.cdf(_d1))
    return float(norm.cdf(_d1) - 1.0)


def gamma(spot: float, strike: float, t: float, rate: float, sigma: float) -> float:
    """Identical for calls and puts (same underlying convexity)."""
    t_eff = max(t, MIN_T)
    sigma_eff = max(sigma, MIN_SIGMA)
    _d1 = d1(spot, strike, t, rate, sigma)
    return float(norm.pdf(_d1) / (spot * sigma_eff * math.sqrt(t_eff)))


def vega(spot: float, strike: float, t: float, rate: float, sigma: float) -> float:
    """Identical for calls and puts. Per 1 vol point (divided by 100)."""
    t_eff = max(t, MIN_T)
    _d1 = d1(spot, strike, t, rate, sigma)
    return float(spot * norm.pdf(_d1) * math.sqrt(t_eff) / 100.0)


def theta(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> float:
    """Per calendar day (annual theta / 365)."""
    t_eff = max(t, MIN_T)
    sigma_eff = max(sigma, MIN_SIGMA)
    _d1 = d1(spot, strike, t, rate, sigma)
    _d2 = d2(spot, strike, t, rate, sigma)
    disc_strike = strike * math.exp(-rate * t_eff)
    decay_term = -(spot * norm.pdf(_d1) * sigma_eff) / (2 * math.sqrt(t_eff))
    if right == OptionRight.CALL:
        rate_term = -rate * disc_strike * norm.cdf(_d2)
    else:
        rate_term = rate * disc_strike * norm.cdf(-_d2)
    return float((decay_term + rate_term) / 365.0)


def rho(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> float:
    """Per 1% rate move (divided by 100)."""
    t_eff = max(t, MIN_T)
    _d2 = d2(spot, strike, t, rate, sigma)
    disc_strike = strike * math.exp(-rate * t_eff)
    if right == OptionRight.CALL:
        return float(disc_strike * t_eff * norm.cdf(_d2) / 100.0)
    return float(-disc_strike * t_eff * norm.cdf(-_d2) / 100.0)


def all_greeks(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> Greeks:
    return Greeks(
        delta=delta(spot, strike, t, rate, sigma, right),
        gamma=gamma(spot, strike, t, rate, sigma),
        theta=theta(spot, strike, t, rate, sigma, right),
        vega=vega(spot, strike, t, rate, sigma),
        rho=rho(spot, strike, t, rate, sigma, right),
    )


_CONTRACT_MULTIPLIER = 100  # shares per option contract


def net_greeks(
    legs: list[Leg], spot: float, t: float, rate: float, sigma: float, underlying_shares: int = 0
) -> Greeks:
    """Position-level greeks in share-equivalent units, so option legs
    and an underlying share position are directly additive: each option
    contract's per-share greek (the convention `all_greeks` returns) is
    scaled by 100 shares/contract before being signed-summed (a short
    leg contributes the negative of a long leg's exposure), and each
    underlying share contributes a flat +1 delta and zero
    gamma/theta/vega/rho. E.g. a covered call's net delta comes out as
    `100 - 100 * call_delta`, matching the standard "position deltas"
    figure a desk would quote."""
    net = Greeks(delta=float(underlying_shares), gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    for leg in legs:
        sign = 1 if leg.side == Side.BUY else -1
        g = all_greeks(spot, leg.strike, t, rate, sigma, leg.right)
        weight = sign * leg.quantity * _CONTRACT_MULTIPLIER
        net = Greeks(
            delta=net.delta + weight * g.delta,
            gamma=net.gamma + weight * g.gamma,
            theta=net.theta + weight * g.theta,
            vega=net.vega + weight * g.vega,
            rho=net.rho + weight * g.rho,
        )
    return net
