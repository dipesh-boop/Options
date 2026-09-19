"""Implied volatility tests: round-trip (price at a known sigma, solve
for IV, recover the same sigma) across a wide range of moneyness/DTE/vol
combinations — an independently verifiable case because the "expected"
IV is exactly the sigma used to generate the price, not a looked-up
number.
"""
from __future__ import annotations

import pytest

from src.quant.black_scholes import OptionRight, price
from src.quant.volatility import implied_volatility

ROUND_TRIP_CASES = [
    # spot, strike, t, rate, true_sigma
    (100.0, 100.0, 0.25, 0.04, 0.20),  # ATM
    (100.0, 100.0, 0.5, 0.04, 0.50),  # ATM, high vol
    (100.0, 80.0, 0.25, 0.03, 0.25),  # ITM call / OTM put
    (100.0, 120.0, 0.25, 0.03, 0.25),  # OTM call / ITM put
    (50.0, 55.0, 0.0833, 0.045, 0.80),  # short-dated, very high vol
    (560.0, 550.0, 2.0, 0.04, 0.14),  # long-dated, low vol
    (100.0, 100.0, 0.02, 0.04, 0.30),  # very short-dated ATM
]


class TestRoundTrip:
    @pytest.mark.parametrize("spot,strike,t,rate,true_sigma", ROUND_TRIP_CASES)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_recovers_the_generating_volatility(self, spot, strike, t, rate, true_sigma, right):
        market_price = price(spot, strike, t, rate, true_sigma, right)
        recovered = implied_volatility(market_price, spot, strike, t, rate, right)
        assert recovered is not None
        assert recovered == pytest.approx(true_sigma, abs=1e-4)

    def test_recovered_iv_reprices_to_the_same_market_price(self):
        spot, strike, t, rate, true_sigma = 100.0, 105.0, 0.3, 0.04, 0.35
        market_price = price(spot, strike, t, rate, true_sigma, OptionRight.CALL)
        recovered = implied_volatility(market_price, spot, strike, t, rate, OptionRight.CALL)
        repriced = price(spot, strike, t, rate, recovered, OptionRight.CALL)
        assert repriced == pytest.approx(market_price, abs=1e-4)


class TestInvalidInputs:
    def test_price_below_intrinsic_value_returns_none(self):
        # A call struck at 80 with spot 100 has intrinsic value 20; a
        # quoted price of 5 is not arbitrage-free.
        result = implied_volatility(5.0, 100.0, 80.0, 0.25, 0.04, OptionRight.CALL)
        assert result is None

    def test_zero_time_to_expiry_returns_none(self):
        result = implied_volatility(10.0, 100.0, 100.0, 0.0, 0.04, OptionRight.CALL)
        assert result is None

    def test_negative_price_returns_none(self):
        result = implied_volatility(-1.0, 100.0, 100.0, 0.25, 0.04, OptionRight.PUT)
        assert result is None


class TestMonotonicity:
    def test_higher_price_implies_higher_volatility(self):
        spot, strike, t, rate = 100.0, 100.0, 0.25, 0.04
        low_price = price(spot, strike, t, rate, 0.15, OptionRight.CALL)
        high_price = price(spot, strike, t, rate, 0.45, OptionRight.CALL)
        iv_low = implied_volatility(low_price, spot, strike, t, rate, OptionRight.CALL)
        iv_high = implied_volatility(high_price, spot, strike, t, rate, OptionRight.CALL)
        assert iv_low < iv_high
