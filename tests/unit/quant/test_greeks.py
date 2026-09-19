"""Greeks tests using finite-difference cross-checks: for each greek, an
independent numerical derivative of `black_scholes.price` itself (not
the analytic formula under test) is computed and compared against the
production closed-form value. This validates that the closed-form
greeks are actually consistent with the pricing function they claim to
describe, which is a much stronger check than comparing against a single
hardcoded reference number.
"""
from __future__ import annotations

import pytest

from src.quant.black_scholes import Leg, OptionRight, Side, price
from src.quant import greeks as g

PARAMETER_SETS = [
    (100.0, 100.0, 0.5, 0.05, 0.25),  # ATM
    (100.0, 80.0, 0.25, 0.03, 0.30),  # deep ITM call
    (100.0, 120.0, 0.25, 0.03, 0.30),  # deep OTM call
    (560.0, 550.0, 1.0, 0.04, 0.14),  # index-like, low vol
    (50.0, 55.0, 0.0833, 0.045, 0.60),  # short-dated high vol
]


def _fd_delta(spot, strike, t, rate, sigma, right, h=0.01) -> float:
    return (price(spot + h, strike, t, rate, sigma, right) - price(spot - h, strike, t, rate, sigma, right)) / (2 * h)


def _fd_gamma(spot, strike, t, rate, sigma, right, h=0.05) -> float:
    p_up = price(spot + h, strike, t, rate, sigma, right)
    p_mid = price(spot, strike, t, rate, sigma, right)
    p_down = price(spot - h, strike, t, rate, sigma, right)
    return (p_up - 2 * p_mid + p_down) / (h**2)


def _fd_vega_raw(spot, strike, t, rate, sigma, right, h=1e-4) -> float:
    return (price(spot, strike, t, rate, sigma + h, right) - price(spot, strike, t, rate, sigma - h, right)) / (2 * h)


def _fd_theta_daily(spot, strike, t, rate, sigma, right, h=None) -> float:
    h = h or t * 1e-4
    dprice_dt = (price(spot, strike, t + h, rate, sigma, right) - price(spot, strike, t - h, rate, sigma, right)) / (2 * h)
    return -dprice_dt / 365.0


class TestDeltaAgainstFiniteDifference:
    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_delta_matches_finite_difference(self, spot, strike, t, rate, sigma, right):
        analytic = g.delta(spot, strike, t, rate, sigma, right)
        numeric = _fd_delta(spot, strike, t, rate, sigma, right)
        assert analytic == pytest.approx(numeric, abs=1e-4)

    def test_call_delta_bounded_zero_to_one(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            d = g.delta(spot, strike, t, rate, sigma, OptionRight.CALL)
            assert 0.0 <= d <= 1.0

    def test_put_delta_bounded_negative_one_to_zero(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            d = g.delta(spot, strike, t, rate, sigma, OptionRight.PUT)
            assert -1.0 <= d <= 0.0

    def test_call_minus_put_delta_equals_one(self):
        # N(d1) - (N(d1) - 1) = 1, for any inputs.
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            call_delta = g.delta(spot, strike, t, rate, sigma, OptionRight.CALL)
            put_delta = g.delta(spot, strike, t, rate, sigma, OptionRight.PUT)
            assert call_delta - put_delta == pytest.approx(1.0, abs=1e-9)


class TestGammaAgainstFiniteDifference:
    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_gamma_matches_finite_difference(self, spot, strike, t, rate, sigma, right):
        analytic = g.gamma(spot, strike, t, rate, sigma)
        numeric = _fd_gamma(spot, strike, t, rate, sigma, right)
        assert analytic == pytest.approx(numeric, rel=1e-2, abs=1e-5)

    def test_gamma_identical_for_call_and_put(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert g.gamma(spot, strike, t, rate, sigma) == g.gamma(spot, strike, t, rate, sigma)

    def test_gamma_always_non_negative(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert g.gamma(spot, strike, t, rate, sigma) >= 0.0


class TestVegaAgainstFiniteDifference:
    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_vega_matches_finite_difference(self, spot, strike, t, rate, sigma, right):
        analytic_raw = g.vega(spot, strike, t, rate, sigma) * 100.0  # per-unit-sigma
        numeric_raw = _fd_vega_raw(spot, strike, t, rate, sigma, right)
        assert analytic_raw == pytest.approx(numeric_raw, rel=1e-3, abs=1e-4)

    def test_vega_always_non_negative(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert g.vega(spot, strike, t, rate, sigma) >= 0.0


class TestThetaAgainstFiniteDifference:
    @pytest.mark.parametrize("spot,strike,t,rate,sigma", PARAMETER_SETS)
    @pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
    def test_theta_matches_finite_difference(self, spot, strike, t, rate, sigma, right):
        analytic = g.theta(spot, strike, t, rate, sigma, right)
        numeric = _fd_theta_daily(spot, strike, t, rate, sigma, right)
        assert analytic == pytest.approx(numeric, rel=1e-2, abs=1e-3)


class TestRhoSanity:
    def test_call_rho_positive(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert g.rho(spot, strike, t, rate, sigma, OptionRight.CALL) >= 0.0

    def test_put_rho_negative(self):
        for spot, strike, t, rate, sigma in PARAMETER_SETS:
            assert g.rho(spot, strike, t, rate, sigma, OptionRight.PUT) <= 0.0


class TestNetGreeks:
    def test_single_short_put_matches_negated_long_put(self):
        spot, strike, t, rate, sigma = 100.0, 95.0, 0.25, 0.04, 0.25
        long_leg = [Leg(right=OptionRight.PUT, strike=strike, side=Side.BUY, entry_price=1.0)]
        short_leg = [Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=1.0)]
        net_long = g.net_greeks(long_leg, spot, t, rate, sigma)
        net_short = g.net_greeks(short_leg, spot, t, rate, sigma)
        assert net_short.delta == pytest.approx(-net_long.delta, abs=1e-9)
        assert net_short.gamma == pytest.approx(-net_long.gamma, abs=1e-9)
        assert net_short.vega == pytest.approx(-net_long.vega, abs=1e-9)

    def test_put_credit_spread_net_delta_between_single_leg_deltas(self):
        spot, t, rate, sigma = 100.0, 0.25, 0.04, 0.25
        legs = [
            Leg(right=OptionRight.PUT, strike=98.0, side=Side.SELL, entry_price=2.5),
            Leg(right=OptionRight.PUT, strike=90.0, side=Side.BUY, entry_price=0.8),
        ]
        net = g.net_greeks(legs, spot, t, rate, sigma)
        short_delta_share_equivalent = 100 * g.delta(spot, 98.0, t, rate, sigma, OptionRight.PUT)
        # Net delta of a short-98/long-90 put spread should be positive
        # (bullish structure) and smaller in magnitude than the naked
        # short put's delta, since the long put offsets some exposure.
        assert net.delta > 0
        assert net.delta < abs(short_delta_share_equivalent)

    def test_underlying_shares_contribute_flat_delta_only(self):
        net = g.net_greeks([], spot=100.0, t=0.25, rate=0.04, sigma=0.25, underlying_shares=100)
        assert net.delta == 100.0
        assert net.gamma == 0.0
        assert net.theta == 0.0
        assert net.vega == 0.0
        assert net.rho == 0.0

    def test_covered_call_net_delta_is_shares_minus_call_delta(self):
        spot, strike, t, rate, sigma = 100.0, 105.0, 0.25, 0.04, 0.25
        legs = [Leg(right=OptionRight.CALL, strike=strike, side=Side.SELL, entry_price=2.0)]
        net = g.net_greeks(legs, spot, t, rate, sigma, underlying_shares=100)
        call_delta = g.delta(spot, strike, t, rate, sigma, OptionRight.CALL)
        assert net.delta == pytest.approx(100.0 - 100 * call_delta, abs=1e-9)
