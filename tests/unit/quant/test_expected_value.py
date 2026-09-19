"""Strategy economics tests. Expected values are written as direct
textbook-formula expressions in each test (e.g. `(strike - credit) *
100`), not hardcoded numbers copied from production output — so a test
failure here means the implementation disagrees with the formula, not
that someone updated a magic constant to match a bug.
"""
from __future__ import annotations

import pytest

from src.quant.expected_value import (
    covered_call_breakeven,
    covered_call_economics,
    covered_call_max_loss,
    covered_call_max_profit,
    csp_breakeven,
    csp_economics,
    csp_max_loss,
    csp_max_profit,
    put_credit_spread_breakeven,
    put_credit_spread_economics,
    put_credit_spread_max_loss,
    put_credit_spread_max_profit,
)
from src.quant.probability import probability_of_profit


class TestCashSecuredPutFormulas:
    def test_max_profit_is_credit_times_100(self):
        assert csp_max_profit(credit=2.50, contracts=1) == pytest.approx(2.50 * 100)

    def test_max_profit_scales_with_contracts(self):
        assert csp_max_profit(credit=2.50, contracts=3) == pytest.approx(2.50 * 100 * 3)

    def test_max_loss_is_strike_minus_credit_times_100(self):
        assert csp_max_loss(strike=100.0, credit=2.50, contracts=1) == pytest.approx((100.0 - 2.50) * 100)

    def test_breakeven_is_strike_minus_credit(self):
        assert csp_breakeven(strike=100.0, credit=2.50) == pytest.approx(97.50)

    def test_max_loss_floored_at_zero_for_credit_exceeding_strike(self):
        # Degenerate/unrealistic input, but the formula must not go
        # negative: a max loss can never be less than zero.
        assert csp_max_loss(strike=1.0, credit=5.0, contracts=1) == 0.0


class TestCoveredCallFormulas:
    def test_max_profit_formula(self):
        strike, credit, cost_basis = 110.0, 3.0, 105.0
        expected = (strike - cost_basis + credit) * 100
        assert covered_call_max_profit(strike, credit, cost_basis) == pytest.approx(expected)

    def test_max_loss_formula(self):
        cost_basis, credit = 105.0, 3.0
        expected = (cost_basis - credit) * 100
        assert covered_call_max_loss(cost_basis, credit) == pytest.approx(expected)

    def test_breakeven_formula(self):
        assert covered_call_breakeven(cost_basis=105.0, credit=3.0) == pytest.approx(102.0)


class TestPutCreditSpreadFormulas:
    def test_max_profit_is_credit_times_100(self):
        assert put_credit_spread_max_profit(credit=1.50) == pytest.approx(150.0)

    def test_max_loss_is_width_minus_credit_times_100(self):
        short_strike, long_strike, credit = 100.0, 95.0, 1.50
        width = short_strike - long_strike
        expected = (width - credit) * 100
        assert put_credit_spread_max_loss(short_strike, long_strike, credit) == pytest.approx(expected)

    def test_breakeven_is_short_strike_minus_credit(self):
        assert put_credit_spread_breakeven(short_strike=100.0, credit=1.50) == pytest.approx(98.50)

    def test_invalid_strike_order_rejected(self):
        with pytest.raises(ValueError):
            put_credit_spread_max_loss(short_strike=95.0, long_strike=100.0, credit=1.0)


class TestReturnOnCapitalAndAnnualization:
    def test_csp_roc_and_annualized_roc(self):
        result = csp_economics(
            spot=105.0, strike=100.0, credit=2.50, t=30 / 365, rate=0.04, sigma=0.25, days_to_expiry=30
        )
        expected_capital = 100.0 * 100
        expected_max_profit = 2.50 * 100
        expected_roc = expected_max_profit / expected_capital
        expected_annualized = expected_roc * (365.0 / 30)
        assert result.capital_required == pytest.approx(expected_capital)
        assert result.return_on_capital == pytest.approx(expected_roc)
        assert result.annualized_roc == pytest.approx(expected_annualized)

    def test_put_credit_spread_capital_required_is_max_loss(self):
        result = put_credit_spread_economics(
            spot=101.0, short_strike=100.0, long_strike=95.0, credit=1.50,
            t=21 / 365, rate=0.04, sigma=0.30, days_to_expiry=21,
        )
        assert result.capital_required == pytest.approx(result.max_loss)

    def test_zero_days_to_expiry_rejected(self):
        with pytest.raises(ValueError):
            csp_economics(spot=100.0, strike=95.0, credit=1.0, t=0.01, rate=0.04, sigma=0.25, days_to_expiry=0)


class TestExpectedValueComposition:
    """expected_value must exactly equal pop*max_profit -
    (1-pop)*max_loss, and the pop used must exactly match
    src.quant.probability.probability_of_profit at the strategy's own
    breakeven — both independently re-derivable from the returned
    object's other fields."""

    def test_csp_expected_value_composition(self):
        spot, strike, credit, t, rate, sigma = 105.0, 100.0, 2.50, 30 / 365, 0.04, 0.25
        result = csp_economics(spot, strike, credit, t, rate, sigma, days_to_expiry=30)
        expected_pop = probability_of_profit(spot, csp_breakeven(strike, credit), t, rate, sigma)
        assert result.probability_of_profit == pytest.approx(expected_pop)
        expected_ev = expected_pop * result.max_profit - (1 - expected_pop) * result.max_loss
        assert result.expected_value == pytest.approx(expected_ev)

    def test_covered_call_expected_value_composition(self):
        spot, strike, credit, cost_basis, t, rate, sigma = 106.0, 110.0, 3.0, 105.0, 30 / 365, 0.04, 0.25
        result = covered_call_economics(spot, strike, credit, cost_basis, t, rate, sigma, days_to_expiry=30)
        expected_pop = probability_of_profit(spot, covered_call_breakeven(cost_basis, credit), t, rate, sigma)
        expected_ev = expected_pop * result.max_profit - (1 - expected_pop) * result.max_loss
        assert result.expected_value == pytest.approx(expected_ev)

    def test_put_credit_spread_expected_value_composition(self):
        spot, short_k, long_k, credit, t, rate, sigma = 101.0, 100.0, 95.0, 1.50, 21 / 365, 0.04, 0.30
        result = put_credit_spread_economics(spot, short_k, long_k, credit, t, rate, sigma, days_to_expiry=21)
        expected_pop = probability_of_profit(spot, put_credit_spread_breakeven(short_k, credit), t, rate, sigma)
        expected_ev = expected_pop * result.max_profit - (1 - expected_pop) * result.max_loss
        assert result.expected_value == pytest.approx(expected_ev)

    def test_higher_probability_of_profit_yields_higher_expected_value(self):
        # Same strategy/strikes, but spot far above strike (safe) vs spot
        # just above breakeven (risky) — expected value must be higher
        # in the safer case.
        common = dict(strike=100.0, credit=2.50, t=30 / 365, rate=0.04, sigma=0.25, days_to_expiry=30)
        safe = csp_economics(spot=130.0, **common)
        risky = csp_economics(spot=98.0, **common)
        assert safe.probability_of_profit > risky.probability_of_profit
        assert safe.expected_value > risky.expected_value
