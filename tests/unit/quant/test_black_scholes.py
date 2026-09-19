"""Black-Scholes pricing tests using independently verifiable
mathematical cases — not just "does it run":

1. An independent N(x) implementation (via `math.erf`, not
   `scipy.stats.norm` — a genuinely different code path from
   production) used to hand-verify call/put prices for a battery of
   parameter sets.
2. Put-call parity (C - P = S - K*e^{-rT}): an exact, model-independent
   no-arbitrage identity that must hold regardless of the specific
   inputs, checked across many parameter combinations.
3. Boundary behavior at/after expiration collapsing to intrinsic value.
"""
from __future__ import annotations

import math

import pytest

from src.quant.black_scholes import OptionRight, intrinsic_value, price


def _independent_norm_cdf(x: float) -> float:
    """Standard normal CDF via the textbook erf identity — deliberately
    not using scipy, so this is a genuinely independent check of the
    production code's `scipy.stats.norm.cdf` calls."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _independent_bs_price(spot: float, strike: float, t: float, rate: float, sigma: float, right: OptionRight) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    disc_strike = strike * math.exp(-rate * t)
    if right == OptionRight.CALL:
        return spot * _independent_norm_cdf(d1) - disc_strike * _independent_norm_cdf(d2)
    return disc_strike * _independent_norm_cdf(-d2) - spot * _independent_norm_cdf(-d1)


PARAMETER_SETS = [
    # spot, strike, t (years), rate, sigma
    (42.0, 40.0, 0.5, 0.10, 0.20),  # Hull's classic textbook example
    (100.0, 100.0, 1.0, 0.05, 0.25),  # ATM, 1yr
    (100.0, 80.0, 0.25, 0.03, 0.30),  # deep ITM call / OTM put
    (100.0, 120.0, 0.25, 0.03, 0.30),  # OTM call / ITM put
    (50.0, 55.0, 0.0833, 0.045, 0.60),  # short-dated, high vol
    (560.0, 550.0, 2.0, 0.04, 0.14),  # long-dated, low vol (index-like)
]


class TestIndependentErfCrossCheck:
    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_matches_independent_implementation(self, spot, strike, t, rate, sigma, right):
        production = price(spot, strike, t, rate, sigma, right)
        independent = _independent_bs_price(spot, strike, t, rate, sigma, right)
        assert production == pytest.approx(independent, abs=1e-10)

    def test_hull_textbook_reference_value(self):
        # Hull, "Options, Futures, and Other Derivatives": S=42, K=40,
        # r=10%, sigma=20%, T=0.5 -> call price ~= 4.76
        call = price(42.0, 40.0, 0.5, 0.10, 0.20, OptionRight.CALL)
        assert call == pytest.approx(4.76, abs=0.01)


class TestPutCallParity:
    """C - P = S - K*e^{-rT} holds exactly under Black-Scholes,
    independent of any specific numeric expectation — a structural
    identity, not a lookup value."""

    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    def test_parity_holds(self, spot, strike, t, rate, sigma):
        call = price(spot, strike, t, rate, sigma, OptionRight.CALL)
        put = price(spot, strike, t, rate, sigma, OptionRight.PUT)
        lhs = call - put
        rhs = spot - strike * math.exp(-rate * t)
        assert lhs == pytest.approx(rhs, abs=1e-9)


class TestExpirationBoundary:
    @pytest.mark.parametrize(
        "spot,strike,expected",
        [(110.0, 100.0, 10.0), (90.0, 100.0, 0.0), (100.0, 100.0, 0.0)],
    )
    def test_call_at_expiration_equals_intrinsic(self, spot, strike, expected):
        assert price(spot, strike, 0.0, 0.05, 0.20, OptionRight.CALL) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "spot,strike,expected",
        [(90.0, 100.0, 10.0), (110.0, 100.0, 0.0), (100.0, 100.0, 0.0)],
    )
    def test_put_at_expiration_equals_intrinsic(self, spot, strike, expected):
        assert price(spot, strike, 0.0, 0.05, 0.20, OptionRight.PUT) == pytest.approx(expected)

    def test_matches_intrinsic_value_helper(self):
        assert price(110.0, 100.0, 0.0, 0.05, 0.20, OptionRight.CALL) == intrinsic_value(110.0, 100.0, OptionRight.CALL)


class TestPriceSanity:
    def test_call_price_never_negative(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert price(spot, strike, t, rate, sigma, OptionRight.CALL) >= 0.0

    def test_put_price_never_negative(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert price(spot, strike, t, rate, sigma, OptionRight.PUT) >= 0.0

    def test_call_price_never_exceeds_spot(self):
        # A call can never be worth more than the underlying itself.
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert price(spot, strike, t, rate, sigma, OptionRight.CALL) <= spot

    def test_deep_itm_call_approaches_intrinsic(self):
        # Deep ITM, short-dated: time value should be small relative to intrinsic.
        c = price(200.0, 50.0, 0.01, 0.05, 0.20, OptionRight.CALL)
        assert c == pytest.approx(150.0, abs=1.0)

    def test_invalid_spot_or_strike_rejected(self):
        with pytest.raises(ValueError):
            price(0.0, 100.0, 0.5, 0.05, 0.2, OptionRight.CALL)
        with pytest.raises(ValueError):
            price(100.0, -1.0, 0.5, 0.05, 0.2, OptionRight.CALL)
