"""Step 22.2 Part 23: Wheel risk-analytics tests -- stress scenarios at
the required shock grid, and aggregate exposure (reserved cash + shares
+ covered-call obligation), never exempted from concentration limits."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.wheel import lifecycle
from src.wheel.risk import (
    WHEEL_STRESS_SPOT_SHOCKS,
    compute_wheel_aggregate_exposure,
    current_wheel_quant_position,
    stress_test_wheel,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestCurrentWheelQuantPosition:
    def test_candidate_has_no_position(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        assert current_wheel_quant_position(w) is None

    def test_csp_open_returns_short_put(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        pos = current_wheel_quant_position(w)
        assert pos is not None
        assert len(pos.legs) == 1
        assert pos.legs[0].strike == 50.0

    def test_shares_held_with_no_cc_returns_shares_only(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        pos = current_wheel_quant_position(w)
        assert pos.underlying_shares == 100
        assert pos.legs == []

    def test_shares_with_open_cc_includes_short_call_leg(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        w = lifecycle.mark_cc_eligible(w, now=NOW)
        w = lifecycle.open_cc(w, strike=53.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id=None, now=NOW, below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        pos = current_wheel_quant_position(w)
        assert len(pos.legs) == 1
        assert pos.underlying_shares == 100


class TestStressTestWheel:
    def test_no_position_has_zero_stress(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        result = stress_test_wheel(w, spot=100.0, sigma=0.25, t=0.1, rate=0.04)
        assert result.scenarios == ()
        assert result.worst_case_loss == 0.0

    def test_short_put_worst_loss_grows_with_deeper_shocks(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=95.0, expiration=date(2026, 2, 5), contracts=1, premium_per_share=1.0, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        result = stress_test_wheel(w, spot=100.0, sigma=0.25, t=35 / 365, rate=0.04)
        assert len(result.scenarios) == len(WHEEL_STRESS_SPOT_SHOCKS) * 3
        loss_at_5 = max(-s.pnl for s in result.scenarios if s.spot_shock_pct == -0.05)
        loss_at_50 = max(-s.pnl for s in result.scenarios if s.spot_shock_pct == -0.50)
        assert loss_at_50 > loss_at_5
        assert result.worst_case_loss > 0

    def test_covers_the_required_shock_grid(self):
        assert WHEEL_STRESS_SPOT_SHOCKS == (-0.05, -0.10, -0.20, -0.30, -0.50)


class TestAggregateExposure:
    def test_csp_open_exposure_equals_reserved_cash(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        pf = Portfolio(as_of=NOW, nav=100_000, cash=90_000, peak_equity=100_000, sector_by_ticker={"SPY": "ETF"})
        exposure = compute_wheel_aggregate_exposure(w, current_underlying_price=51.0, portfolio=pf, sector="ETF", limits=get_default_limits())
        assert exposure.reserved_csp_cash == 5000.0
        assert exposure.shares_market_value == 0.0
        assert exposure.total_capital_at_risk == 5000.0

    def test_shares_held_exposure_is_market_value_not_strike(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        pf = Portfolio(as_of=NOW, nav=100_000, cash=90_000, peak_equity=100_000, sector_by_ticker={"SPY": "ETF"})
        exposure = compute_wheel_aggregate_exposure(w, current_underlying_price=45.0, portfolio=pf, sector="ETF", limits=get_default_limits())
        assert exposure.reserved_csp_cash == 0.0
        assert exposure.shares_market_value == 4500.0  # 100 shares * $45, not the $50 strike
        assert exposure.total_capital_at_risk == 4500.0

    def test_open_cc_obligation_shares_tracked(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        w = lifecycle.mark_cc_eligible(w, now=NOW)
        w = lifecycle.open_cc(w, strike=53.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id=None, now=NOW, below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        pf = Portfolio(as_of=NOW, nav=100_000, cash=90_000, peak_equity=100_000, sector_by_ticker={"SPY": "ETF"})
        exposure = compute_wheel_aggregate_exposure(w, current_underlying_price=51.0, portfolio=pf, sector="ETF", limits=get_default_limits())
        assert exposure.covered_call_obligation_shares == 100

    def test_exposure_feeds_the_same_concentration_check_every_other_strategy_uses(self):
        # Never a special, looser Wheel-only rule (Part 12) -- the exact
        # same underlying_exposure_pct/sector_exposure_pct functions
        # src.risk.concentration itself calls.
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=20, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        pf = Portfolio(as_of=NOW, nav=100_000, cash=90_000, peak_equity=100_000, sector_by_ticker={"SPY": "ETF"})
        exposure = compute_wheel_aggregate_exposure(w, current_underlying_price=51.0, portfolio=pf, sector="ETF", limits=get_default_limits())
        # 20 contracts * 50 * 100 = $100,000 -- exceeds NAV entirely, well
        # past the 10% underlying concentration limit.
        assert exposure.underlying_exposure_pct_of_nav > get_default_limits().max_underlying_exposure_pct
