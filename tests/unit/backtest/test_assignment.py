"""Tests for expiration settlement: intrinsic value, assignment on a
short leg, exercise on a long leg, and OTM worthless expiry -- covering
all three of this platform's strategies (CSP, covered call, PCS)."""
from __future__ import annotations

import pytest

from src.backtest.assignment import intrinsic_value, realized_settlement_pnl, settle_leg, settle_position
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


class TestRealizedSettlementPnlRegressionOP001:
    """OP-001: raw `cash_impact` is the full strike notional, which
    corrupts realized P&L by ignoring the value of the resulting stock
    position. `realized_settlement_pnl` must report the much smaller,
    intrinsic-value-based economic impact instead."""

    def test_otm_leg_contributes_nothing(self):
        legs = (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),)
        settlements = settle_position(list(legs), contracts=1, settlement_price=100.0)
        assert realized_settlement_pnl(settlements, contracts=1) == 0.0

    def test_naked_short_put_assignment_is_bounded_by_intrinsic_value_not_full_notional(self):
        """Cash-secured put, strike 95, settling at 90 (only $5 ITM).
        The full strike notional (95*100=9,500) must never appear as the
        settlement's economic impact -- only the $500 intrinsic loss."""
        legs = (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),)
        settlements = settle_position(list(legs), contracts=1, settlement_price=90.0)
        impact = realized_settlement_pnl(settlements, contracts=1)
        assert impact == pytest.approx(-5.0 * _MULT)
        assert impact != pytest.approx(-95.0 * _MULT)  # the OP-001 bug's old (wrong) value

    def test_covered_call_assignment_uses_actual_cost_basis_not_intrinsic_value(self):
        """Covered call: shares held at cost basis 100, call strike 105,
        settling at 115. Realized gain must be (105-100)*100=500 (the
        actual cost-basis gain), not the option's own intrinsic value
        (10*100=1,000) and not the full strike notional (105*100=10,500)."""
        legs = (BacktestLeg(right=OptionRight.CALL, strike=105.0, side="sell"),)
        settlements = settle_position(list(legs), contracts=1, settlement_price=115.0)
        impact = realized_settlement_pnl(settlements, contracts=1, underlying_shares_held=100, underlying_cost_basis=100.0)
        assert impact == pytest.approx((105.0 - 100.0) * _MULT)
        assert impact != pytest.approx(10.0 * _MULT)  # intrinsic-value-only would be wrong here too
        assert impact != pytest.approx(105.0 * _MULT)  # the OP-001 bug's old (wrong) value

    def test_covered_call_with_zero_shares_held_falls_back_to_intrinsic_value(self):
        """Without underlying_shares_held set, there is no cost basis to
        realize against -- falls back to the same intrinsic-value
        treatment as any other short leg."""
        legs = (BacktestLeg(right=OptionRight.CALL, strike=105.0, side="sell"),)
        settlements = settle_position(list(legs), contracts=1, settlement_price=115.0)
        impact = realized_settlement_pnl(settlements, contracts=1)
        assert impact == pytest.approx(-10.0 * _MULT)

    def test_put_credit_spread_both_legs_itm_matches_max_loss_unchanged(self):
        """A fully-assigned vertical spread's shares always net to flat
        -- this is the one shape the old cash_impact-sum formula already
        got right, and the fix must not regress it."""
        legs = (
            BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),
            BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy"),
        )
        settlements = settle_position(list(legs), contracts=1, settlement_price=85.0)
        impact = realized_settlement_pnl(settlements, contracts=1)
        assert impact == pytest.approx(-(95.0 - 90.0) * _MULT)

    def test_scales_with_contracts(self):
        legs = (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),)
        settlements = settle_position(list(legs), contracts=3, settlement_price=90.0)
        impact = realized_settlement_pnl(settlements, contracts=3)
        assert impact == pytest.approx(-5.0 * _MULT * 3)
