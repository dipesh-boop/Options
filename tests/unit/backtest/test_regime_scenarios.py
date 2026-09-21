"""Tests for src.backtest.regime_scenarios (the 8-regime scenario
generator) plus the cross-strategy property Step 19A actually asks for:
"do not require one strategy to always win in any regime" -- checked
here as "every defined-risk strategy's maximum_loss bound (already
proven exact by test_payoff_profile.py) holds against every simulated
terminal price across all 8 regimes," never as a P&L sign assertion."""
from __future__ import annotations

import math

import pytest

from src.backtest.regime_scenarios import ALL_REGIME_SCENARIOS, RegimeScenario, generate_regime_path
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position, payoff_at_expiration, payoff_profile


class TestGenerateRegimePath:
    def test_all_8_regimes_generate_a_valid_path(self):
        assert len(ALL_REGIME_SCENARIOS) == 8
        for regime in ALL_REGIME_SCENARIOS:
            path = generate_regime_path(regime, start_price=100.0, num_days=30, seed=1)
            assert len(path.prices) == 30
            assert len(path.implied_vols) == 30
            assert all(p > 0 for p in path.prices)

    def test_deterministic_given_same_seed(self):
        p1 = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=20, seed=42)
        p2 = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=20, seed=42)
        assert p1.prices == p2.prices

    def test_different_seeds_produce_different_paths(self):
        p1 = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=20, seed=1)
        p2 = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=20, seed=2)
        assert p1.prices != p2.prices

    def test_market_crash_has_a_large_single_day_drop(self):
        path = generate_regime_path(RegimeScenario.MARKET_CRASH, start_price=100.0, num_days=30, seed=7)
        daily_returns = [path.prices[i + 1] / path.prices[i] - 1.0 for i in range(len(path.prices) - 1)]
        assert min(daily_returns) < -0.15  # the crash day's jump dominates

    def test_bull_trends_up_on_average_bear_trends_down(self):
        bull = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=252, seed=3)
        bear = generate_regime_path(RegimeScenario.BEAR, start_price=100.0, num_days=252, seed=3)
        assert bull.prices[-1] > bull.prices[0]
        assert bear.prices[-1] < bear.prices[0]

    def test_high_volatility_regime_has_wider_price_dispersion_than_low_volatility(self):
        high_vol = generate_regime_path(RegimeScenario.HIGH_VOLATILITY, start_price=100.0, num_days=100, seed=5)
        low_vol = generate_regime_path(RegimeScenario.LOW_VOLATILITY, start_price=100.0, num_days=100, seed=5)
        high_vol_range = max(high_vol.prices) - min(high_vol.prices)
        low_vol_range = max(low_vol.prices) - min(low_vol.prices)
        assert high_vol_range > low_vol_range

    def test_rejects_invalid_inputs(self):
        with pytest.raises(ValueError):
            generate_regime_path(RegimeScenario.BULL, start_price=0.0, num_days=10, seed=1)
        with pytest.raises(ValueError):
            generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=1, seed=1)


class TestNoStrategyRequiredToWinInEveryRegime:
    """The actual falsifiable property behind "do not require one
    strategy to always win in any regime": a defined-risk strategy's
    own maximum_loss bound (proven exact by payoff_profile) must never
    be exceeded by ANY simulated terminal price in ANY of the 8
    regimes -- but the strategy is not required to be profitable in
    every regime, and this test never asserts that it is."""

    def _put_credit_spread(self) -> Position:
        return Position(legs=[
            Leg(right=OptionRight.PUT, strike=95, side=Side.SELL, entry_price=2.8, quantity=1),
            Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=1.4, quantity=1),
        ])

    @pytest.mark.parametrize("regime", list(RegimeScenario))
    def test_put_credit_spread_never_loses_more_than_its_bound_in_any_regime(self, regime):
        position = self._put_credit_spread()
        profile = payoff_profile(position)
        path = generate_regime_path(regime, start_price=100.0, num_days=60, seed=99)
        for price in path.prices:
            pnl = payoff_at_expiration(position, price)
            assert pnl >= -profile.max_loss - 1e-6, f"{regime}: pnl {pnl} exceeded max_loss bound {profile.max_loss}"

    def test_bull_regime_does_not_guarantee_a_win_for_a_bearish_structure(self):
        """A bear put spread is not required to win in a bull regime --
        this test only proves it stays within its own defined-risk
        bound, exactly like the parametrized test above, for one
        concrete regime chosen to make the point explicit."""
        position = Position(legs=[
            Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=6.0, quantity=1),
            Leg(right=OptionRight.PUT, strike=90, side=Side.SELL, entry_price=2.0, quantity=1),
        ])
        profile = payoff_profile(position)
        path = generate_regime_path(RegimeScenario.BULL, start_price=100.0, num_days=60, seed=11)
        for price in path.prices:
            pnl = payoff_at_expiration(position, price)
            assert pnl >= -profile.max_loss - 1e-6
        # Deliberately no assertion about whether this structure made or
        # lost money in this regime -- that is exactly the outcome this
        # property test must never require in either direction.
