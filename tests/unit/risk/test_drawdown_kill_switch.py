"""Tests for src.risk.drawdown and src.risk.kill_switch."""
from __future__ import annotations

import pytest

from src.risk.drawdown import DrawdownZone, current_drawdown_pct, drawdown_zone, sizing_multiplier_for_zone
from src.risk.kill_switch import check_kill_switch
from src.risk.limits import load_risk_limits
from src.risk.reason_codes import ReasonCode
from tests.unit.risk.conftest import make_portfolio


LIMITS = load_risk_limits()


class TestDrawdownZones:
    @pytest.mark.parametrize(
        "nav,peak,expected_zone",
        [
            (100_000, 100_000, DrawdownZone.NORMAL),
            (93_000, 100_000, DrawdownZone.NORMAL),  # 7% < 8% warning
            (91_000, 100_000, DrawdownZone.WARNING),  # 9%, in [8%, 10%)
            (89_000, 100_000, DrawdownZone.RISK_REDUCTION),  # 11%, in [10%, 15%)
            (84_000, 100_000, DrawdownZone.HALT),  # 16% >= 15%
            (85_000, 100_000, DrawdownZone.HALT),  # exactly 15% -> halt (>=)
        ],
    )
    def test_zone_boundaries(self, nav, peak, expected_zone):
        dd = current_drawdown_pct(make_portfolio(nav=float(nav), cash=float(nav) * 0.9, peak_equity=float(peak)))
        assert drawdown_zone(dd, LIMITS) == expected_zone

    def test_sizing_multiplier_is_1_outside_risk_reduction_zone(self):
        assert sizing_multiplier_for_zone(DrawdownZone.NORMAL, LIMITS) == 1.0
        assert sizing_multiplier_for_zone(DrawdownZone.WARNING, LIMITS) == 1.0
        assert sizing_multiplier_for_zone(DrawdownZone.HALT, LIMITS) == 1.0

    def test_sizing_multiplier_tightens_in_risk_reduction_zone(self):
        assert sizing_multiplier_for_zone(DrawdownZone.RISK_REDUCTION, LIMITS) == LIMITS.risk_reduction_sizing_multiplier
        assert sizing_multiplier_for_zone(DrawdownZone.RISK_REDUCTION, LIMITS) < 1.0


class TestKillSwitch:
    def test_no_halt_under_normal_conditions(self):
        portfolio = make_portfolio(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0)
        result = check_kill_switch(portfolio, LIMITS)
        assert result.halted is False

    def test_automatic_halt_on_drawdown(self):
        portfolio = make_portfolio(nav=80_000.0, cash=70_000.0, peak_equity=100_000.0)
        result = check_kill_switch(portfolio, LIMITS)
        assert result.halted is True
        assert result.reason_code == ReasonCode.HALT_PORTFOLIO_DRAWDOWN

    def test_manual_halt_overrides_even_a_healthy_portfolio(self):
        portfolio = make_portfolio(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, halted=True, halt_reason="operator stop")
        result = check_kill_switch(portfolio, LIMITS)
        assert result.halted is True
        assert result.reason_code == ReasonCode.HALT_MANUAL_KILL_SWITCH

    def test_manual_halt_checked_before_drawdown_math(self):
        # Even a portfolio with zero drawdown halts if the manual flag is set.
        portfolio = make_portfolio(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, halted=True, halt_reason="test")
        result = check_kill_switch(portfolio, LIMITS)
        assert result.reason_code == ReasonCode.HALT_MANUAL_KILL_SWITCH
