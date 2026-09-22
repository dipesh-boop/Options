"""Step 22.2 Part 22/23: Wheel cohort validation-reporting tests."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.validation.wheel_attribution import WheelComparisonBenchmarks, build_wheel_cohort_attribution
from src.wheel import lifecycle

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _completed_wheel_called_away():
    w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    w = lifecycle.csp_assigned(w, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    w = lifecycle.mark_cc_eligible(w, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    w = lifecycle.open_cc(w, strike=53.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id=None, now=datetime(2026, 2, 1, tzinfo=timezone.utc), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
    w = lifecycle.shares_called_away(w, now=datetime(2026, 3, 1, tzinfo=timezone.utc))
    return lifecycle.complete_wheel(w, now=datetime(2026, 3, 1, tzinfo=timezone.utc))


def _completed_wheel_expired_worthless():
    w = lifecycle.open_wheel_candidate(wheel_id="w2", ticker="QQQ", now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    w = lifecycle.open_csp(w, strike=40.0, expiration=date(2026, 2, 5), contracts=1, premium_per_share=0.9, commission=0.65, proposal_id="p3", position_id=None, now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    return lifecycle.csp_expires_worthless(w, now=datetime(2026, 2, 5, tzinfo=timezone.utc))


def _below_basis_wheel():
    w = lifecycle.open_wheel_candidate(wheel_id="w3", ticker="IWM", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.0, commission=0.65, proposal_id="p4", position_id=None, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    w = lifecycle.csp_assigned(w, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    w = lifecycle.mark_cc_eligible(w, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    return lifecycle.open_cc(
        w, strike=45.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=1.5, commission=0.65,
        proposal_id="p5", position_id=None, now=datetime(2026, 2, 1, tzinfo=timezone.utc),
        below_acquisition_basis=True, below_economic_basis=False, max_loss_if_called_away=500.0,
    )


class TestEmptyCohort:
    def test_no_wheels_never_raises_a_division_error(self):
        result = build_wheel_cohort_attribution([], now=NOW)
        assert result.wheels_initiated == 0
        assert result.assignment_rate is None
        assert result.average_total_wheel_return_pct is None


class TestNoWinRateField:
    def test_win_rate_is_not_a_field_at_all(self):
        result = build_wheel_cohort_attribution([_completed_wheel_called_away()], now=NOW)
        assert not hasattr(result, "win_rate")


class TestBasicCohortMath:
    def test_assignment_rate_and_called_away_frequency(self):
        wheels = [_completed_wheel_called_away(), _completed_wheel_expired_worthless()]
        result = build_wheel_cohort_attribution(wheels, now=NOW)
        assert result.wheels_initiated == 2
        assert result.wheels_assigned == 1
        assert result.assignment_rate == 0.5
        assert result.called_away_frequency == 1.0  # 1 of the 1 assigned wheel was called away

    def test_expectancy_reflects_total_net_pnl_over_finished_wheels(self):
        wheels = [_completed_wheel_called_away(), _completed_wheel_expired_worthless()]
        result = build_wheel_cohort_attribution(wheels, now=NOW)
        assert result.expectancy_per_wheel is not None
        assert result.expectancy_per_wheel > 0  # both wheels here are net winners

    def test_below_basis_calls_counted(self):
        result = build_wheel_cohort_attribution([_below_basis_wheel()], now=NOW)
        assert result.below_basis_call_count == 1
        assert result.below_basis_call_rate == 1.0
        assert result.losses_not_avoided_by_call_count == 1

    def test_comparisons_pass_through_unmodified(self):
        comps = WheelComparisonBenchmarks(buy_and_hold_return_pct=0.05, standalone_csp_return_pct=0.03, cash_return_pct=0.02, spy_benchmark_return_pct=0.04)
        result = build_wheel_cohort_attribution([_completed_wheel_called_away()], now=NOW, comparisons=comps)
        assert result.comparisons == comps

    def test_in_progress_wheel_counted_as_initiated_but_not_in_return_averages(self):
        in_progress = lifecycle.open_wheel_candidate(wheel_id="w4", ticker="SPY", now=NOW)
        in_progress = lifecycle.open_csp(in_progress, strike=50.0, expiration=date(2026, 4, 1), contracts=1, premium_per_share=1.0, commission=0.65, proposal_id="p6", position_id=None, now=NOW)
        result = build_wheel_cohort_attribution([in_progress], now=NOW)
        assert result.wheels_initiated == 1
        assert result.average_total_wheel_return_pct is None  # no *finished* wheels yet
