"""Tests for the 9 new src.quant.expected_value strategy-economics
functions added in Step 19A -- every assertion is a direct textbook
formula, cross-checked where relevant against src.quant.monte_carlo
.payoff_profile's independently-derived (exact, not approximate) engine."""
from __future__ import annotations

import math

import pytest

from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.expected_value import (
    bear_put_spread_economics,
    bull_call_spread_economics,
    call_credit_spread_economics,
    long_call_economics,
    long_put_economics,
    long_straddle_economics,
    long_strangle_economics,
    protective_collar_economics,
    protective_put_economics,
)
from src.quant.monte_carlo import Position, payoff_profile

_COMMON = dict(t=30 / 365, rate=0.04, sigma=0.25, days_to_expiry=30)


class TestCallCreditSpread:
    def test_matches_payoff_profile(self):
        e = call_credit_spread_economics(spot=100, short_strike=105, long_strike=110, credit=1.6, **_COMMON)
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=105, side=Side.SELL, entry_price=3.2, quantity=1),
            Leg(right=OptionRight.CALL, strike=110, side=Side.BUY, entry_price=1.6, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert e.max_profit == pytest.approx(pf.max_profit)
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])

    def test_rejects_inverted_strikes(self):
        with pytest.raises(ValueError):
            call_credit_spread_economics(spot=100, short_strike=110, long_strike=105, credit=1.0, **_COMMON)


class TestBullCallSpread:
    def test_matches_payoff_profile(self):
        e = bull_call_spread_economics(spot=100, long_strike=95, short_strike=105, debit=4.5, **_COMMON)
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=95, side=Side.BUY, entry_price=6.9, quantity=1),
            Leg(right=OptionRight.CALL, strike=105, side=Side.SELL, entry_price=2.4, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert e.max_profit == pytest.approx(pf.max_profit)
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])


class TestBearPutSpread:
    def test_matches_payoff_profile(self):
        e = bear_put_spread_economics(spot=100, long_strike=100, short_strike=90, debit=4.0, **_COMMON)
        pos = Position(legs=[
            Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=6.0, quantity=1),
            Leg(right=OptionRight.PUT, strike=90, side=Side.SELL, entry_price=2.0, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert e.max_profit == pytest.approx(pf.max_profit)
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])


class TestLongCall:
    def test_unbounded_max_profit_and_matches_payoff_profile_loss(self):
        e = long_call_economics(spot=100, strike=100, premium=3.0, **_COMMON)
        assert e.max_profit == math.inf
        pos = Position(legs=[Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1)])
        pf = payoff_profile(pos)
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])


class TestLongPut:
    def test_bounded_max_profit_matches_payoff_profile(self):
        e = long_put_economics(spot=100, strike=100, premium=3.0, **_COMMON)
        pos = Position(legs=[Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=3.0, quantity=1)])
        pf = payoff_profile(pos)
        assert e.max_profit == pytest.approx(pf.max_profit)
        assert e.max_loss == pytest.approx(pf.max_loss)


class TestProtectivePut:
    def test_matches_payoff_profile(self):
        e = protective_put_economics(spot=100, put_strike=90, premium=2.0, cost_basis=95.0, **_COMMON)
        pos = Position(
            legs=[Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=2.0, quantity=1)],
            underlying_shares=100, underlying_cost_basis=95.0,
        )
        pf = payoff_profile(pos)
        assert e.max_profit == math.inf
        assert e.max_loss == pytest.approx(pf.max_loss)


class TestProtectiveCollar:
    def test_matches_payoff_profile_net_credit(self):
        e = protective_collar_economics(spot=100, call_strike=110, put_strike=90, net_credit=0.5, cost_basis=95.0, **_COMMON)
        pos = Position(
            legs=[
                Leg(right=OptionRight.CALL, strike=110, side=Side.SELL, entry_price=2.0, quantity=1),
                Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=1.5, quantity=1),
            ],
            underlying_shares=100, underlying_cost_basis=95.0,
        )
        pf = payoff_profile(pos)
        assert e.max_profit == pytest.approx(pf.max_profit)
        assert e.max_loss == pytest.approx(pf.max_loss)

    def test_both_profit_and_loss_are_capped(self):
        e = protective_collar_economics(spot=100, call_strike=110, put_strike=90, net_credit=-0.5, cost_basis=95.0, **_COMMON)
        assert math.isfinite(e.max_profit)
        assert math.isfinite(e.max_loss)


class TestLongStraddle:
    def test_matches_payoff_profile_two_breakevens(self):
        e = long_straddle_economics(spot=100, strike=100, call_premium=3.0, put_premium=3.2, **_COMMON)
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1),
            Leg(right=OptionRight.PUT, strike=100, side=Side.BUY, entry_price=3.2, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert e.max_profit == math.inf
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])
        assert e.breakeven_upper == pytest.approx(pf.breakeven_points[1])


class TestLongStrangle:
    def test_matches_payoff_profile_two_breakevens(self):
        e = long_strangle_economics(spot=100, call_strike=105, put_strike=95, call_premium=1.5, put_premium=1.5, **_COMMON)
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=105, side=Side.BUY, entry_price=1.5, quantity=1),
            Leg(right=OptionRight.PUT, strike=95, side=Side.BUY, entry_price=1.5, quantity=1),
        ])
        pf = payoff_profile(pos)
        assert e.max_profit == math.inf
        assert e.max_loss == pytest.approx(pf.max_loss)
        assert e.breakeven == pytest.approx(pf.breakeven_points[0])
        assert e.breakeven_upper == pytest.approx(pf.breakeven_points[1])

    def test_rejects_inverted_strikes(self):
        with pytest.raises(ValueError):
            long_strangle_economics(spot=100, call_strike=95, put_strike=105, call_premium=1.5, put_premium=1.5, **_COMMON)


class TestContractsScaling:
    def test_bull_call_spread_scales_with_contracts(self):
        e1 = bull_call_spread_economics(spot=100, long_strike=95, short_strike=105, debit=4.5, contracts=1, **_COMMON)
        e5 = bull_call_spread_economics(spot=100, long_strike=95, short_strike=105, debit=4.5, contracts=5, **_COMMON)
        assert e5.max_profit == pytest.approx(e1.max_profit * 5)
        assert e5.max_loss == pytest.approx(e1.max_loss * 5)
        assert e5.breakeven == pytest.approx(e1.breakeven)
