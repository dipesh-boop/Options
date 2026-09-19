"""Black-Scholes pricing, Greeks, and implied-volatility solver.

IBKR's market-data ticks already include model greeks/IV, but we compute
them ourselves so the tool works identically against the mock provider,
stays correct if a feed omits a greek, and lets us show a house view
that's consistent across every contract.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from scipy.stats import norm

from app.data.models import OptionContract, Right

_MIN_T = 1e-6  # years; avoids division by zero for same-day expiry
_MIN_SIGMA = 1e-4


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point (1%)
    rho: float  # per 1% rate move
    implied_vol: float


def years_to_expiry(expiry: date, as_of: date | None = None) -> float:
    as_of = as_of or date.today()
    days = (expiry - as_of).days
    return max(days, 0) / 365.0


def _d1_d2(spot: float, strike: float, t: float, rate: float, sigma: float) -> tuple[float, float]:
    sigma = max(sigma, _MIN_SIGMA)
    t = max(t, _MIN_T)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def bs_price(spot: float, strike: float, t: float, rate: float, sigma: float, right: Right) -> float:
    """Black-Scholes European option price."""
    if t <= _MIN_T:
        intrinsic = (spot - strike) if right == Right.CALL else (strike - spot)
        return max(intrinsic, 0.0)

    d1, d2 = _d1_d2(spot, strike, t, rate, sigma)
    disc_k = strike * math.exp(-rate * t)
    if right == Right.CALL:
        return spot * norm.cdf(d1) - disc_k * norm.cdf(d2)
    return disc_k * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_greeks(spot: float, strike: float, t: float, rate: float, sigma: float, right: Right) -> Greeks:
    """Analytic Black-Scholes greeks. `implied_vol` is set to the input `sigma`."""
    t_eff = max(t, _MIN_T)
    sigma_eff = max(sigma, _MIN_SIGMA)
    d1, d2 = _d1_d2(spot, strike, t_eff, rate, sigma_eff)
    pdf_d1 = norm.pdf(d1)
    sqrt_t = math.sqrt(t_eff)
    disc_k = strike * math.exp(-rate * t_eff)

    gamma = pdf_d1 / (spot * sigma_eff * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1 vol point

    if right == Right.CALL:
        delta = norm.cdf(d1)
        theta_year = -(spot * pdf_d1 * sigma_eff) / (2 * sqrt_t) - rate * disc_k * norm.cdf(d2)
        rho = disc_k * t_eff * norm.cdf(d2) / 100.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta_year = -(spot * pdf_d1 * sigma_eff) / (2 * sqrt_t) + rate * disc_k * norm.cdf(-d2)
        rho = -disc_k * t_eff * norm.cdf(-d2) / 100.0

    if t <= _MIN_T:
        # Expired/expiring: greeks collapse to their limiting values.
        delta = 1.0 if (right == Right.CALL and spot > strike) else (
            -1.0 if (right == Right.PUT and spot < strike) else 0.0
        )
        gamma = vega = theta_year = rho = 0.0

    return Greeks(
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        theta=round(theta_year / 365.0, 4),
        vega=round(vega, 4),
        rho=round(rho, 4),
        implied_vol=round(sigma, 4),
    )


def implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    t: float,
    rate: float,
    right: Right,
    *,
    initial_guess: float = 0.4,
    max_iterations: int = 50,
    tolerance: float = 1e-5,
) -> float | None:
    """Solve for implied volatility via Newton-Raphson with a bisection
    fallback. Returns None if the price is outside any arbitrage-free bound
    or the solver fails to converge."""
    if t <= _MIN_T:
        return None

    intrinsic = (spot - strike) if right == Right.CALL else (strike - spot)
    if market_price < max(intrinsic, 0.0) - 1e-6:
        return None  # below intrinsic value: not a valid price

    sigma = initial_guess
    for _ in range(max_iterations):
        price = bs_price(spot, strike, t, rate, sigma, right)
        vega = bs_greeks(spot, strike, t, rate, sigma, right).vega * 100.0  # back to per-unit-sigma
        diff = price - market_price
        if abs(diff) < tolerance:
            return round(max(sigma, _MIN_SIGMA), 4)
        if vega < 1e-8:
            break
        sigma -= diff / vega
        if sigma <= 0 or sigma > 5:
            break
    else:
        price = bs_price(spot, strike, t, rate, sigma, right)
        if abs(price - market_price) < tolerance:
            return round(max(sigma, _MIN_SIGMA), 4)

    # Bisection fallback over a wide, sane vol range.
    lo, hi = _MIN_SIGMA, 5.0
    price_lo = bs_price(spot, strike, t, rate, lo, right)
    price_hi = bs_price(spot, strike, t, rate, hi, right)
    if not (price_lo <= market_price <= price_hi):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        price_mid = bs_price(spot, strike, t, rate, mid, right)
        if abs(price_mid - market_price) < tolerance:
            return round(mid, 4)
        if price_mid < market_price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def compute_contract_greeks(
    contract: OptionContract,
    underlying_price: float,
    risk_free_rate: float,
    as_of: date | None = None,
) -> Greeks | None:
    """Compute IV + greeks for a contract from its mid price. Returns None
    if IV can't be solved (e.g. stale/crossed quote)."""
    t = years_to_expiry(contract.expiry, as_of)
    iv = implied_volatility(contract.mid, underlying_price, contract.strike, t, risk_free_rate, contract.right)
    if iv is None:
        return None
    return bs_greeks(underlying_price, contract.strike, t, risk_free_rate, iv, contract.right)
