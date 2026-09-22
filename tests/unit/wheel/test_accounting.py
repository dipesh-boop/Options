"""Step 22.2 Part 23: Wheel accounting tests -- premium, commissions,
stock basis (both acquisition and economic), realized/unrealized P&L,
called-away P&L, multiple CC cycles."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.wheel import accounting as wacc
from src.wheel.models import WheelAccounting, WheelPosition
from src.wheel.state import WheelState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _wheel(**overrides) -> WheelPosition:
    base = dict(wheel_id="w1", ticker="SPY", state=WheelState.WHEEL_CANDIDATE, started_at=NOW)
    base.update(overrides)
    return WheelPosition(**base)


class TestCspLifecycleAccounting:
    def test_csp_opened_reserves_full_strike_times_100_times_contracts(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=2)
        assert acc.capital_committed == 10_000.0
        assert acc.max_capital_committed == 10_000.0

    def test_csp_premium_received_scales_by_shares_and_contracts(self):
        acc = wacc.csp_premium_received(WheelAccounting(), premium_per_share=1.25, contracts=2, commission=1.30)
        assert acc.total_csp_premium == 250.0
        assert acc.total_commissions == 1.30

    def test_csp_released_unassigned_frees_reserved_cash_and_floors_at_zero(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=1)
        acc = wacc.csp_released_unassigned(acc, strike=50.0, contracts=1, realized_pnl=119.35)
        assert acc.capital_committed == 0.0
        assert acc.realized_option_pnl == 119.35

    def test_csp_released_unassigned_never_goes_negative_on_mismatched_release(self):
        acc = WheelAccounting(capital_committed=100.0)
        acc = wacc.csp_released_unassigned(acc, strike=50.0, contracts=1, realized_pnl=0.0)
        assert acc.capital_committed == 0.0  # floored, not -4900


class TestAssignmentAccounting:
    def test_assigned_sets_acquisition_basis_to_strike(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=1)
        acc = wacc.csp_premium_received(acc, premium_per_share=1.2, contracts=1, commission=0.65)
        acc = wacc.assigned(acc, strike=50.0, contracts=1, assigned_at=NOW)
        assert acc.shares_owned == 100
        assert acc.gross_stock_acquisition_cost == 5000.0
        assert acc.acquisition_basis_per_share == 50.0

    def test_economic_basis_is_acquisition_basis_minus_premium_per_share(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=1)
        acc = wacc.csp_premium_received(acc, premium_per_share=1.2, contracts=1, commission=0.0)
        acc = wacc.assigned(acc, strike=50.0, contracts=1, assigned_at=NOW)
        # 120 total premium / 100 shares = 1.20/share -> 50 - 1.20 = 48.80
        assert acc.economic_basis_per_share == pytest.approx(48.80)

    def test_economic_basis_never_confused_with_acquisition_basis(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=1)
        acc = wacc.csp_premium_received(acc, premium_per_share=1.2, contracts=1, commission=0.0)
        acc = wacc.assigned(acc, strike=50.0, contracts=1, assigned_at=NOW)
        assert acc.economic_basis_per_share != acc.acquisition_basis_per_share

    def test_multiple_assignment_cycles_weight_average_the_acquisition_basis(self):
        acc = WheelAccounting()
        acc = wacc.assigned(acc, strike=50.0, contracts=1, assigned_at=NOW)
        acc = wacc.assigned(acc, strike=60.0, contracts=1, assigned_at=NOW)
        # (50*100 + 60*100) / 200 shares = 55.0
        assert acc.acquisition_basis_per_share == 55.0
        assert acc.shares_owned == 200

    def test_shares_acquired_at_set_once_and_not_overwritten_by_a_second_assignment(self):
        first = NOW
        later = datetime(2026, 2, 1, tzinfo=timezone.utc)
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=first)
        acc = wacc.assigned(acc, strike=55.0, contracts=1, assigned_at=later)
        assert acc.shares_acquired_at == first


class TestCoveredCallAccounting:
    def test_cc_opened_reserves_no_additional_cash(self):
        acc = WheelAccounting(shares_owned=100, capital_committed=5000.0)
        acc2 = wacc.cc_opened(acc)
        assert acc2.capital_committed == acc.capital_committed

    def test_cc_premium_further_reduces_economic_basis(self):
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        basis_before = acc.economic_basis_per_share
        acc = wacc.cc_premium_received(acc, premium_per_share=0.8, contracts=1, commission=0.65)
        assert acc.economic_basis_per_share < basis_before
        assert acc.total_cc_premium == 80.0
        assert acc.total_commissions == 0.65

    def test_multiple_cc_cycles_cumulatively_reduce_economic_basis(self):
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        acc = wacc.cc_premium_received(acc, premium_per_share=0.5, contracts=1, commission=0.0)
        after_first = acc.economic_basis_per_share
        acc = wacc.cc_premium_received(acc, premium_per_share=0.5, contracts=1, commission=0.0)
        assert acc.economic_basis_per_share < after_first
        assert acc.economic_basis_per_share == pytest.approx(50.0 - 1.0)

    def test_cc_closed_unassigned_only_realizes_option_pnl_shares_untouched(self):
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        shares_before = acc.shares_owned
        acc = wacc.cc_closed_unassigned(acc, realized_pnl=45.0)
        assert acc.shares_owned == shares_before
        assert acc.realized_option_pnl == 45.0


class TestCalledAwayAccounting:
    def test_called_away_realizes_gain_against_acquisition_basis(self):
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        acc = wacc.called_away(acc, strike=53.0, contracts=1)
        assert acc.realized_stock_pnl == 300.0  # (53-50)*100
        assert acc.shares_owned == 0
        assert acc.capital_committed == 0.0

    def test_called_away_below_basis_realizes_a_loss(self):
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        acc = wacc.called_away(acc, strike=45.0, contracts=1)
        assert acc.realized_stock_pnl == -500.0

    def test_called_away_without_acquisition_basis_raises(self):
        with pytest.raises(ValueError):
            wacc.called_away(WheelAccounting(), strike=50.0, contracts=1)

    def test_called_away_never_uses_economic_basis_for_the_stock_pnl(self):
        # Economic basis (premium-adjusted) must never substitute for the
        # real tax-style acquisition basis when computing what was
        # actually gained/lost on the stock itself (Part 6/9).
        acc = wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW)
        acc = wacc.cc_premium_received(acc, premium_per_share=2.0, contracts=1, commission=0.0)
        assert acc.economic_basis_per_share == 48.0
        acc = wacc.called_away(acc, strike=49.0, contracts=1)
        # If economic basis (48) were wrongly used: (49-48)*100 = 100 (a gain).
        # Correct (acquisition basis 50): (49-50)*100 = -100 (a loss).
        assert acc.realized_stock_pnl == -100.0


class TestBelowBasisFlags:
    def test_strike_above_both_bases_flags_neither(self):
        below_acq, below_econ = wacc.below_basis_flags(55.0, acquisition_basis_per_share=50.0, economic_basis_per_share=48.0)
        assert not below_acq and not below_econ

    def test_strike_between_economic_and_acquisition_basis_flags_only_acquisition(self):
        below_acq, below_econ = wacc.below_basis_flags(49.0, acquisition_basis_per_share=50.0, economic_basis_per_share=48.0)
        assert below_acq and not below_econ

    def test_strike_below_both_flags_both(self):
        below_acq, below_econ = wacc.below_basis_flags(47.0, acquisition_basis_per_share=50.0, economic_basis_per_share=48.0)
        assert below_acq and below_econ

    def test_missing_basis_never_flags(self):
        below_acq, below_econ = wacc.below_basis_flags(47.0, acquisition_basis_per_share=None, economic_basis_per_share=None)
        assert not below_acq and not below_econ


class TestMaxLossIfCalledAway:
    def test_strike_above_basis_is_zero_loss(self):
        assert wacc.max_loss_if_called_away(55.0, 50.0, 100) == 0.0

    def test_strike_below_basis_is_positive_loss(self):
        assert wacc.max_loss_if_called_away(45.0, 50.0, 100) == 500.0


class TestSummarizeWheelEconomics:
    def test_unrealized_pnl_computed_against_acquisition_basis(self):
        w = _wheel(state=WheelState.CC_ELIGIBLE, accounting=wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW))
        summary = wacc.summarize_wheel_economics(w, current_underlying_price=55.0, now=NOW)
        assert summary.unrealized_stock_pnl == 500.0
        assert summary.current_market_value == 5500.0

    def test_no_current_price_leaves_unrealized_and_market_value_at_zero_and_none(self):
        w = _wheel(state=WheelState.CC_ELIGIBLE, accounting=wacc.assigned(WheelAccounting(), strike=50.0, contracts=1, assigned_at=NOW))
        summary = wacc.summarize_wheel_economics(w, current_underlying_price=None, now=NOW)
        assert summary.unrealized_stock_pnl == 0.0
        assert summary.current_market_value is None

    def test_roc_and_annualized_roc(self):
        acc = wacc.csp_opened(WheelAccounting(), strike=50.0, contracts=1)
        acc = wacc.csp_premium_received(acc, premium_per_share=1.2, contracts=1, commission=0.65)
        acc = wacc.csp_released_unassigned(acc, strike=50.0, contracts=1, realized_pnl=119.35)
        started = NOW
        completed = datetime(2026, 1, 31, tzinfo=timezone.utc)  # 30 days later
        w = _wheel(state=WheelState.CSP_EXPIRED, started_at=started, completed_at=completed, accounting=acc)
        summary = wacc.summarize_wheel_economics(w, current_underlying_price=None, now=completed)
        assert summary.days_in_wheel == 30
        assert summary.return_on_committed_capital == pytest.approx(119.35 / 5000.0)
        assert summary.annualized_return_on_committed_capital == pytest.approx((119.35 / 5000.0) * (365.0 / 30.0))

    def test_naive_datetime_rejected(self):
        w = _wheel()
        with pytest.raises(ValueError):
            wacc.summarize_wheel_economics(w, current_underlying_price=None, now=datetime(2026, 1, 2))

    def test_days_holding_stock_zero_before_any_assignment(self):
        w = _wheel(state=WheelState.WHEEL_CANDIDATE)
        summary = wacc.summarize_wheel_economics(w, current_underlying_price=None, now=NOW)
        assert summary.days_holding_stock == 0
