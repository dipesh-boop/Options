"""Probability calculation tests: boundary cases at expiration,
complementarity identities (P(above) + P(below) = 1, exactly, by
construction), and monotonicity in strike/threshold — all independently
verifiable without any external reference value."""
from __future__ import annotations

import pytest

from src.quant.black_scholes import OptionRight
from src.quant.probability import probability_above, probability_below, probability_itm, probability_of_profit

PARAMETER_SETS = [
    (100.0, 100.0, 0.25, 0.04, 0.25),
    (100.0, 80.0, 0.25, 0.03, 0.30),
    (100.0, 120.0, 0.25, 0.03, 0.30),
    (560.0, 550.0, 1.0, 0.04, 0.14),
]


class TestComplementarity:
    @pytest.mark.parametrize("spot,threshold,t,rate,sigma", PARAMETER_SETS)
    def test_above_and_below_sum_to_one(self, spot, threshold, t, rate, sigma):
        above = probability_above(spot, threshold, t, rate, sigma)
        below = probability_below(spot, threshold, t, rate, sigma)
        assert above + below == pytest.approx(1.0, abs=1e-12)

    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    def test_call_and_put_itm_probability_sum_to_one(self, spot, strike, t, rate, sigma):
        call_itm = probability_itm(spot, strike, t, rate, sigma, OptionRight.CALL)
        put_itm = probability_itm(spot, strike, t, rate, sigma, OptionRight.PUT)
        assert call_itm + put_itm == pytest.approx(1.0, abs=1e-12)

    def test_probabilities_are_bounded_zero_one(self):
        for spot, threshold, t, rate, sigma in PARAMETER_SETS:
            assert 0.0 <= probability_above(spot, threshold, t, rate, sigma) <= 1.0


class TestExpirationBoundary:
    def test_above_threshold_at_expiration_is_certain(self):
        assert probability_above(110.0, 100.0, 0.0, 0.04, 0.25) == 1.0

    def test_below_threshold_at_expiration_is_impossible(self):
        assert probability_above(90.0, 100.0, 0.0, 0.04, 0.25) == 0.0

    def test_call_itm_at_expiration_matches_actual_moneyness(self):
        assert probability_itm(110.0, 100.0, 0.0, 0.04, 0.25, OptionRight.CALL) == 1.0
        assert probability_itm(90.0, 100.0, 0.0, 0.04, 0.25, OptionRight.CALL) == 0.0


class TestMonotonicity:
    def test_call_itm_probability_decreases_as_strike_rises(self):
        spot, t, rate, sigma = 100.0, 0.25, 0.04, 0.25
        probs = [probability_itm(spot, k, t, rate, sigma, OptionRight.CALL) for k in (80.0, 100.0, 120.0)]
        assert probs[0] > probs[1] > probs[2]

    def test_put_itm_probability_increases_as_strike_rises(self):
        spot, t, rate, sigma = 100.0, 0.25, 0.04, 0.25
        probs = [probability_itm(spot, k, t, rate, sigma, OptionRight.PUT) for k in (80.0, 100.0, 120.0)]
        assert probs[0] < probs[1] < probs[2]


class TestProbabilityOfProfit:
    def test_bullish_profit_zone_matches_probability_above(self):
        spot, breakeven, t, rate, sigma = 100.0, 95.0, 0.25, 0.04, 0.25
        assert probability_of_profit(spot, breakeven, t, rate, sigma) == probability_above(
            spot, breakeven, t, rate, sigma
        )

    def test_bearish_profit_zone_matches_probability_below(self):
        spot, breakeven, t, rate, sigma = 100.0, 105.0, 0.25, 0.04, 0.25
        assert probability_of_profit(
            spot, breakeven, t, rate, sigma, bullish=False
        ) == probability_below(spot, breakeven, t, rate, sigma)

    def test_lower_breakeven_means_higher_probability_of_profit_for_bullish(self):
        spot, t, rate, sigma = 100.0, 0.25, 0.04, 0.25
        pop_easy = probability_of_profit(spot, 90.0, t, rate, sigma)  # breakeven well below spot
        pop_hard = probability_of_profit(spot, 99.0, t, rate, sigma)  # breakeven near spot
        assert pop_easy > pop_hard
