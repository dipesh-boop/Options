"""Tests for `src.quant.monte_carlo.payoff_profile` -- the generic,
exact (not approximate) engine every src.strategies module builds on.
Every case here is hand-derivable from textbook payoff formulas,
matching this package's own "independently verifiable" standard."""
from __future__ import annotations

import math

import pytest

from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position, payoff_profile


class TestSingleLeg:
    def test_long_call(self):
        pos = Position(legs=[Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1)])
        pf = payoff_profile(pos)
        assert pf.max_profit == math.inf
        assert pf.max_loss == pytest.approx(300.0)
        assert pf.breakeven_points == pytest.approx((103.0,))
        assert pf.upside_unbounded is True

    def test_long_put(self):
        pos = Position(legs=[Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=3.0, quantity=1)])
        pf = payoff_profile(pos)
        assert pf.max_profit == pytest.approx(9700.0)  # (100-3)*100
        assert pf.max_loss == pytest.approx(300.0)
        assert pf.breakeven_points == pytest.approx((97.0,))
        assert pf.upside_unbounded is False

    def test_cash_secured_put_short(self):
        pos = Position(legs=[Leg(right=OptionRight.PUT, strike=100, side=Side.SELL, entry_price=3.0, quantity=1)])
        pf = payoff_profile(pos)
        assert pf.max_profit == pytest.approx(300.0)
        assert pf.max_loss == pytest.approx(9700.0)
        assert pf.breakeven_points == pytest.approx((97.0,))


class TestVerticalSpreads:
    def test_bull_call_spread(self):
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=95, side=Side.BUY, entry_price=6.9, quantity=1),
            Leg(right=OptionRight.CALL, strike=105, side=Side.SELL, entry_price=2.4, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert pf.max_profit == pytest.approx(550.0)
        assert pf.max_loss == pytest.approx(450.0)
        assert pf.breakeven_points == pytest.approx((99.5,))
        assert pf.upside_unbounded is False
        assert pf.downside_unbounded is False

    def test_put_credit_spread(self):
        pos = Position(legs=[
            Leg(right=OptionRight.PUT, strike=95, side=Side.SELL, entry_price=2.8, quantity=1),
            Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=1.4, quantity=1),
        ])
        pf = payoff_profile(pos)
        credit = 2.8 - 1.4
        width = 5
        assert pf.max_profit == pytest.approx(credit * 100)
        assert pf.max_loss == pytest.approx((width - credit) * 100)
        assert pf.breakeven_points == pytest.approx((95 - credit,))


class TestStraddleAndStrangle:
    def test_long_straddle_two_breakevens(self):
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1),
            Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=3.2, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert pf.max_profit == math.inf
        assert pf.max_loss == pytest.approx(620.0)
        assert len(pf.breakeven_points) == 2
        assert pf.breakeven_points[0] == pytest.approx(93.8)
        assert pf.breakeven_points[1] == pytest.approx(106.2)

    def test_long_strangle_wider_than_straddle_breakevens(self):
        straddle = Position(legs=[
            Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1),
            Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=3.0, quantity=1),
        ])
        strangle = Position(legs=[
            Leg(right=OptionRight.CALL, strike=105, side=Side.BUY, entry_price=1.5, quantity=1),
            Leg(right=OptionRight.PUT, strike=95, side=Side.BUY, entry_price=1.5, quantity=1),
        ])
        pf_straddle = payoff_profile(straddle)
        pf_strangle = payoff_profile(strangle)
        straddle_width = pf_straddle.breakeven_points[1] - pf_straddle.breakeven_points[0]
        strangle_width = pf_strangle.breakeven_points[1] - pf_strangle.breakeven_points[0]
        assert strangle_width > straddle_width  # cheaper structure -> wider required move


class TestMultiLegQuantityRatios:
    def test_long_call_butterfly(self):
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=95, side=Side.BUY, entry_price=6.5, quantity=1),
            Leg(right=OptionRight.CALL, strike=100, side=Side.SELL, entry_price=3.5, quantity=2),
            Leg(right=OptionRight.CALL, strike=105, side=Side.BUY, entry_price=1.5, quantity=1),
        ])
        pf = payoff_profile(pos)
        debit = 6.5 - 2 * 3.5 + 1.5
        assert debit == pytest.approx(1.0)
        assert pf.max_loss == pytest.approx(debit * 100)
        assert pf.max_profit == pytest.approx((5 - debit) * 100)
        assert len(pf.breakeven_points) == 2
        assert pf.breakeven_points[0] == pytest.approx(96.0)
        assert pf.breakeven_points[1] == pytest.approx(104.0)

    def test_short_iron_condor(self):
        pos = Position(legs=[
            Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=0.6, quantity=1),
            Leg(right=OptionRight.PUT, strike=95, side=Side.SELL, entry_price=1.2, quantity=1),
            Leg(right=OptionRight.CALL, strike=105, side=Side.SELL, entry_price=1.1, quantity=1),
            Leg(right=OptionRight.CALL, strike=110, side=Side.BUY, entry_price=0.5, quantity=1),
        ])
        pf = payoff_profile(pos)
        credit = 1.2 + 1.1 - 0.6 - 0.5
        assert pf.max_profit == pytest.approx(credit * 100)
        assert pf.max_loss == pytest.approx((5 - credit) * 100)
        assert len(pf.breakeven_points) == 2

    def test_short_iron_butterfly(self):
        pos = Position(legs=[
            Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=0.8, quantity=1),
            Leg(right=OptionRight.PUT, strike=100, side=Side.SELL, entry_price=3.2, quantity=1),
            Leg(right=OptionRight.CALL, strike=100, side=Side.SELL, entry_price=3.4, quantity=1),
            Leg(right=OptionRight.CALL, strike=110, side=Side.BUY, entry_price=0.9, quantity=1),
        ])
        pf = payoff_profile(pos)
        credit = 3.2 + 3.4 - 0.8 - 0.9
        assert pf.max_profit == pytest.approx(credit * 100)
        assert pf.max_loss == pytest.approx((10 - credit) * 100)
        assert len(pf.breakeven_points) == 2


class TestWithUnderlyingShares:
    def test_covered_call(self):
        pos = Position(
            legs=[Leg(right=OptionRight.CALL, strike=100, side=Side.SELL, entry_price=3.0, quantity=1)],
            underlying_shares=100, underlying_cost_basis=95.0,
        )
        pf = payoff_profile(pos)
        assert pf.max_profit == pytest.approx((100 - 95 + 3) * 100)
        assert pf.max_loss == pytest.approx(max(95 - 3, 0) * 100)
        assert pf.upside_unbounded is False  # capped by the short call

    def test_protective_put(self):
        pos = Position(
            legs=[Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=2.0, quantity=1)],
            underlying_shares=100, underlying_cost_basis=95.0,
        )
        pf = payoff_profile(pos)
        assert pf.max_profit == math.inf  # shares have no cap
        assert pf.max_loss == pytest.approx(max(95 - 90 + 2, 0) * 100)

    def test_long_shares_only(self):
        """Long stock alone: unbounded upside (no short call caps it),
        but a FINITE downside (the stock floors at $0 -- the position's
        loss cannot exceed the cost basis)."""
        pos = Position(legs=[], underlying_shares=100, underlying_cost_basis=95.0)
        pf = payoff_profile(pos)
        assert pf.max_profit == math.inf
        assert pf.max_loss == pytest.approx(95.0 * 100)
        assert pf.downside_unbounded is False

    def test_short_shares_has_unbounded_loss(self):
        """A naked short stock position (negative underlying_shares) has
        genuinely unbounded loss as price rises -- correctly flagged,
        not silently capped."""
        pos = Position(legs=[], underlying_shares=-100, underlying_cost_basis=95.0)
        pf = payoff_profile(pos)
        assert pf.max_loss == math.inf
        assert pf.downside_unbounded is True


class TestEdgeCases:
    def test_raises_on_completely_empty_position(self):
        with pytest.raises(ValueError):
            payoff_profile(Position(legs=[]))

    def test_quantities_scale_linearly(self):
        pos1 = Position(legs=[Leg(right=OptionRight.PUT, strike=95, side=Side.SELL, entry_price=2.8, quantity=1)])
        pos3 = Position(legs=[Leg(right=OptionRight.PUT, strike=95, side=Side.SELL, entry_price=2.8, quantity=3)])
        pf1, pf3 = payoff_profile(pos1), payoff_profile(pos3)
        assert pf3.max_profit == pytest.approx(pf1.max_profit * 3)
        assert pf3.max_loss == pytest.approx(pf1.max_loss * 3)
        assert pf3.breakeven_points == pytest.approx(pf1.breakeven_points)  # breakeven is per-share, quantity-independent
