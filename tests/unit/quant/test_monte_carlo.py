"""Monte Carlo and stress-test tests.

The strongest independent check here is convergence: a Monte Carlo
estimate of a single option's discounted expected payoff must converge
to the closed-form Black-Scholes price as n_paths grows, with the
discrepancy bounded by the simulation's own standard error — this is a
statistically principled tolerance (not an arbitrary epsilon), derived
from the Central Limit Theorem rather than from anything specific to
this codebase.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.quant.black_scholes import Leg, OptionRight, Side, price
from src.quant.monte_carlo import (
    STANDARD_SPOT_SHOCKS,
    STANDARD_VOL_SHOCKS,
    Position,
    monte_carlo_pop_and_ev,
    net_entry_credit,
    payoff_at_expiration,
    simulate_terminal_prices,
    stress_test,
)


class TestSimulateTerminalPrices:
    def test_seeded_simulation_is_reproducible(self):
        a = simulate_terminal_prices(100.0, 0.25, 0.5, 0.04, 10_000, seed=42)
        b = simulate_terminal_prices(100.0, 0.25, 0.5, 0.04, 10_000, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_different_seeds_produce_different_paths(self):
        a = simulate_terminal_prices(100.0, 0.25, 0.5, 0.04, 10_000, seed=1)
        b = simulate_terminal_prices(100.0, 0.25, 0.5, 0.04, 10_000, seed=2)
        assert not np.array_equal(a, b)

    def test_mean_terminal_price_matches_risk_neutral_drift(self):
        # E[S_T] = S0 * e^{rT} under the risk-neutral measure — check
        # with a large sample and a generous but principled tolerance
        # (a few standard errors of the sample mean).
        spot, sigma, t, rate = 100.0, 0.20, 1.0, 0.05
        n = 500_000
        terminal = simulate_terminal_prices(spot, sigma, t, rate, n, seed=7)
        sample_mean = terminal.mean()
        theoretical_mean = spot * math.exp(rate * t)
        sample_se = terminal.std(ddof=1) / math.sqrt(n)
        assert abs(sample_mean - theoretical_mean) < 5 * sample_se

    def test_zero_time_returns_spot_unchanged(self):
        result = simulate_terminal_prices(100.0, 0.25, 0.0, 0.04, 1000, seed=1)
        assert np.all(result == 100.0)

    def test_non_positive_paths_rejected(self):
        with pytest.raises(ValueError):
            simulate_terminal_prices(100.0, 0.25, 0.5, 0.04, 0)


class TestMonteCarloConvergesToBlackScholes:
    def test_single_short_put_price_converges(self):
        spot, strike, t, rate, sigma = 100.0, 95.0, 0.25, 0.04, 0.25
        entry_price = price(spot, strike, t, rate, sigma, OptionRight.PUT)
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=entry_price)])

        result = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, n_paths=200_000, seed=123)

        # A short option sold at its exact fair (risk-neutral) price has
        # zero expected P&L under the risk-neutral measure, discounting
        # aside. We use un-discounted terminal payoffs here, so compare
        # against the (small) forward-value adjustment instead of zero.
        discounted_ev = result.expected_value * math.exp(-rate * t)
        assert abs(discounted_ev) < 5 * result.standard_error

    def test_put_credit_spread_ev_converges_to_analytic_ev_within_error(self):
        spot, short_k, long_k, t, rate, sigma = 101.0, 100.0, 95.0, 0.25, 0.04, 0.30
        short_price = price(spot, short_k, t, rate, sigma, OptionRight.PUT)
        long_price = price(spot, long_k, t, rate, sigma, OptionRight.PUT)
        position = Position(
            legs=[
                Leg(right=OptionRight.PUT, strike=short_k, side=Side.SELL, entry_price=short_price),
                Leg(right=OptionRight.PUT, strike=long_k, side=Side.BUY, entry_price=long_price),
            ]
        )
        result = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, n_paths=200_000, seed=99)
        # Both legs priced at their exact fair value -> the position's
        # risk-neutral discounted expected P&L should be ~0.
        discounted_ev = result.expected_value * math.exp(-rate * t)
        assert abs(discounted_ev) < 5 * result.standard_error


class TestPayoffAtExpirationBoundaries:
    def test_csp_payoff_at_zero_equals_max_loss(self):
        strike, credit = 100.0, 2.50
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=credit)])
        payoff = payoff_at_expiration(position, terminal_price=0.0)
        assert payoff == pytest.approx(-(strike - credit) * 100)

    def test_csp_payoff_far_above_strike_equals_max_profit(self):
        strike, credit = 100.0, 2.50
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=credit)])
        payoff = payoff_at_expiration(position, terminal_price=500.0)
        assert payoff == pytest.approx(credit * 100)

    def test_covered_call_payoff_at_zero_equals_max_loss(self):
        strike, credit, cost_basis = 110.0, 3.0, 105.0
        position = Position(
            legs=[Leg(right=OptionRight.CALL, strike=strike, side=Side.SELL, entry_price=credit)],
            underlying_shares=100,
            underlying_cost_basis=cost_basis,
        )
        payoff = payoff_at_expiration(position, terminal_price=0.0)
        assert payoff == pytest.approx(-(cost_basis - credit) * 100)

    def test_covered_call_payoff_far_above_strike_equals_max_profit(self):
        strike, credit, cost_basis = 110.0, 3.0, 105.0
        position = Position(
            legs=[Leg(right=OptionRight.CALL, strike=strike, side=Side.SELL, entry_price=credit)],
            underlying_shares=100,
            underlying_cost_basis=cost_basis,
        )
        payoff = payoff_at_expiration(position, terminal_price=1000.0)
        assert payoff == pytest.approx((strike - cost_basis + credit) * 100)

    def test_put_credit_spread_payoff_at_zero_equals_max_loss(self):
        short_k, long_k, credit = 100.0, 95.0, 1.50
        position = Position(
            legs=[
                Leg(right=OptionRight.PUT, strike=short_k, side=Side.SELL, entry_price=2.5),
                Leg(right=OptionRight.PUT, strike=long_k, side=Side.BUY, entry_price=1.0),
            ]
        )
        # net credit actually received = 2.5 - 1.0 = 1.5, matches `credit` above
        payoff = payoff_at_expiration(position, terminal_price=0.0)
        width = short_k - long_k
        assert payoff == pytest.approx(-(width - credit) * 100)

    def test_put_credit_spread_payoff_far_above_short_strike_equals_max_profit(self):
        short_k, long_k = 100.0, 95.0
        position = Position(
            legs=[
                Leg(right=OptionRight.PUT, strike=short_k, side=Side.SELL, entry_price=2.5),
                Leg(right=OptionRight.PUT, strike=long_k, side=Side.BUY, entry_price=1.0),
            ]
        )
        payoff = payoff_at_expiration(position, terminal_price=500.0)
        assert payoff == pytest.approx((2.5 - 1.0) * 100)


class TestNetEntryCredit:
    def test_short_leg_is_positive_credit(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=100.0, side=Side.SELL, entry_price=2.5)])
        assert net_entry_credit(position) == pytest.approx(2.5)

    def test_long_leg_is_negative_debit(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=95.0, side=Side.BUY, entry_price=1.0)])
        assert net_entry_credit(position) == pytest.approx(-1.0)

    def test_spread_net_credit_is_short_minus_long(self):
        position = Position(
            legs=[
                Leg(right=OptionRight.PUT, strike=100.0, side=Side.SELL, entry_price=2.5),
                Leg(right=OptionRight.PUT, strike=95.0, side=Side.BUY, entry_price=1.0),
            ]
        )
        assert net_entry_credit(position) == pytest.approx(1.5)


class TestStressTest:
    def test_uses_exactly_the_six_required_spot_shocks_by_default(self):
        assert STANDARD_SPOT_SHOCKS == (-0.20, -0.10, -0.05, 0.05, 0.10, 0.20)

    def test_returns_one_scenario_per_shock_with_no_vol_shock(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=100.0, side=Side.SELL, entry_price=2.5)])
        scenarios = stress_test(position, spot=100.0, sigma=0.25, t=0.25, rate=0.04)
        assert len(scenarios) == len(STANDARD_SPOT_SHOCKS)
        assert {s.spot_shock_pct for s in scenarios} == set(STANDARD_SPOT_SHOCKS)
        assert {s.vol_shock_pct for s in scenarios} == {0.0}

    def test_full_grid_covers_every_spot_and_vol_combination(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=100.0, side=Side.SELL, entry_price=2.5)])
        scenarios = stress_test(
            position, spot=100.0, sigma=0.25, t=0.25, rate=0.04, vol_shocks=STANDARD_VOL_SHOCKS
        )
        assert len(scenarios) == len(STANDARD_SPOT_SHOCKS) * len(STANDARD_VOL_SHOCKS)

    def test_pnl_matches_direct_black_scholes_repricing(self):
        spot, strike, t, rate, sigma = 100.0, 95.0, 0.25, 0.04, 0.25
        entry_price = 2.50
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=entry_price)])
        scenarios = stress_test(position, spot, sigma, t, rate, spot_shocks=(0.10,))
        scenario = scenarios[0]
        shocked_spot = spot * 1.10
        expected_theo = price(shocked_spot, strike, t, rate, sigma, OptionRight.PUT)
        expected_pnl = (entry_price - expected_theo) * 100  # short leg: profit if option cheapens
        assert scenario.pnl == pytest.approx(expected_pnl)

    def test_down_20_percent_is_worse_than_up_20_percent_for_short_put(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=100.0, side=Side.SELL, entry_price=2.5)])
        scenarios = stress_test(position, spot=100.0, sigma=0.25, t=0.25, rate=0.04)
        by_shock = {s.spot_shock_pct: s.pnl for s in scenarios}
        assert by_shock[-0.20] < by_shock[0.20]

    def test_vol_shock_included_in_repricing(self):
        spot, strike, t, rate, sigma = 100.0, 100.0, 0.25, 0.04, 0.25
        entry_price = price(spot, strike, t, rate, sigma, OptionRight.PUT)
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=entry_price)])
        scenarios = stress_test(position, spot, sigma, t, rate, spot_shocks=(0.0,), vol_shocks=(0.30,))
        scenario = scenarios[0]
        assert scenario.shocked_sigma == pytest.approx(sigma * 1.30)
        # A short put loses value (bad for the seller) when IV rises,
        # holding spot fixed.
        assert scenario.pnl < 0
