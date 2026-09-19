"""Tests for expiration settlement: intrinsic value, assignment on a
short leg, exercise on a long leg, and OTM worthless expiry -- covering
all three of this platform's strategies (CSP, covered call, PCS)."""
from __future__ import annotations

import pytest

from src.backtest.assignment import intrinsic_value, settle_leg, settle_position
from src.backtest.simulator import BacktestLeg
from src.data.option_chain import OptionRight

_MULT = 100


class TestIntrinsicValue:
    def test_call_itm(self):
        assert intrinsic_value(OptionRight.CALL, strike=100.0, settlement_price=110.0) == pytest.approx(10.0)

    def test_call_otm_floors_at_zero(self):
        assert intrinsic_value(OptionRight.CALL, strike=100.0, settlement_price=90.0) == 0.0

    def test_put_itm(self):
        assert intrinsic_value(OptionRight.PUT, strike=100.0, settlement_price=90.0) == pytest.approx(10.0)

    def test_put_otm_floors_at_zero(self):
        assert intrinsic_value(OptionRight.PUT, strike=100.0, settlement_price=110.0) == 0.0


class TestSettleLegOtmExpiresWorthless:
    def test_short_put_otm_no_cash_or_share_impact(self):
        leg = BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell")
        settlement = settle_leg(leg, contracts=2, settlement_price=100.0)
        assert settlement.was_itm is False
        assert settlement.assigned_or_exercised is False
        assert settlement.cash_impact == 0.0
        assert settlement.share_impact == 0


class TestSettleLegAssignmentAndExercise:
    def test_short_put_assigned_buys_shares_and_pays_strike(self):
        """Cash-secured put assigned ITM: shares are bought at strike."""
        leg = BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell")
        settlement = settle_leg(leg, contracts=1, settlement_price=90.0)
        assert settlement.was_itm is True
        assert settlement.assigned_or_exercised is True
        assert settlement.cash_impact == pytest.approx(-95.0 * _MULT)
        assert settlement.share_impact == _MULT

    def test_short_call_assigned_sells_shares_and_receives_strike(self):
        """Covered call assigned ITM: shares are called away at strike."""
        leg = BacktestLeg(right=OptionRight.CALL, strike=105.0, side="sell")
        settlement = settle_leg(leg, contracts=1, settlement_price=115.0)
        assert settlement.was_itm is True
        assert settlement.cash_impact == pytest.approx(105.0 * _MULT)
        assert settlement.share_impact == -_MULT

    def test_long_put_exercised_sells_shares(self):
        leg = BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy")
        settlement = settle_leg(leg, contracts=1, settlement_price=80.0)
        assert settlement.was_itm is True
        assert settlement.cash_impact == pytest.approx(90.0 * _MULT)
        assert settlement.share_impact == -_MULT

    def test_long_call_exercised_buys_shares(self):
        leg = BacktestLeg(right=OptionRight.CALL, strike=110.0, side="buy")
        settlement = settle_leg(leg, contracts=1, settlement_price=120.0)
        assert settlement.was_itm is True
        assert settlement.cash_impact == pytest.approx(-110.0 * _MULT)
        assert settlement.share_impact == _MULT

    def test_cash_and_share_impact_scale_with_contracts(self):
        leg = BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell")
        settlement = settle_leg(leg, contracts=3, settlement_price=90.0)
        assert settlement.cash_impact == pytest.approx(-95.0 * _MULT * 3)
        assert settlement.share_impact == _MULT * 3


class TestSettlePositionPutCreditSpread:
    def test_both_legs_itm_short_assigned_long_exercised_nets_to_strike_width_loss(self):
        """A put credit spread (short 95 / long 90) settling at 85 --
        below *both* strikes, so both legs are ITM: short put is
        assigned (pay 95/share), long put is exercised (receive
        90/share) -> net cash impact is -(95-90)*100*contracts, i.e.
        exactly the spread's max loss, independent of credit collected
        at entry (which is accounted for separately)."""
        legs = (
            BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),
            BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy"),
        )
        settlements = settle_position(list(legs), contracts=1, settlement_price=85.0)
        total_cash = sum(s.cash_impact for s in settlements)
        total_shares = sum(s.share_impact for s in settlements)
        assert total_cash == pytest.approx(-(95.0 - 90.0) * _MULT)
        assert total_shares == 0  # short put buys shares, long put sells shares -- they net to flat

    def test_price_between_strikes_only_the_short_leg_settles(self):
        """At 92 (between the two strikes), only the short 95 put is
        ITM -- this is the partial-loss region of the spread's payoff,
        distinct from the max-loss case where both legs settle."""
        legs = (
            BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),
            BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy"),
        )
        settlements = settle_position(list(legs), contracts=1, settlement_price=92.0)
        short_settlement, long_settlement = settlements
        assert short_settlement.assigned_or_exercised is True
        assert long_settlement.assigned_or_exercised is False
        total_cash = sum(s.cash_impact for s in settlements)
        assert total_cash == pytest.approx(-95.0 * _MULT)

    def test_both_legs_otm_expires_fully_worthless(self):
        legs = (
            BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),
            BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy"),
        )
        settlements = settle_position(list(legs), contracts=1, settlement_price=100.0)
        assert all(not s.assigned_or_exercised for s in settlements)
        assert sum(s.cash_impact for s in settlements) == 0.0
