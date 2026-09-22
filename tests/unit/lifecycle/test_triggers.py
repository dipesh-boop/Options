"""Part 4-12/27: every deterministic trigger function, including its
DATA_INSUFFICIENT fail-safe branch (Part 19) and Part 18's precedence
category tagging."""
from __future__ import annotations

from src.lifecycle import triggers as trg
from src.lifecycle.policy import ManagementPolicy
from src.lifecycle.state import PositionLifecycleState as S
from src.strategies.base import StrategyKind


def _policy(**overrides) -> ManagementPolicy:
    base = dict(name="TEST", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, description="test")
    base.update(overrides)
    return ManagementPolicy(**base)


# ------------------------------------------------------------- Part 4: profit

class TestProfitTargetPct:
    def test_not_configured_returns_none(self):
        assert trg.check_profit_target_pct(_policy(), unrealized_pnl=100.0, profit_capture_denominator=100.0) is None

    def test_fires_at_or_above_target(self):
        p = _policy(profit_target_pct=0.5)
        f = trg.check_profit_target_pct(p, unrealized_pnl=60.0, profit_capture_denominator=100.0)
        assert f is not None and f.target_state == S.PROFIT_TARGET_REACHED and f.category == "profit_target" and not f.mandatory

    def test_does_not_fire_below_target(self):
        p = _policy(profit_target_pct=0.5)
        assert trg.check_profit_target_pct(p, unrealized_pnl=40.0, profit_capture_denominator=100.0) is None

    def test_missing_denominator_is_data_insufficient(self):
        p = _policy(profit_target_pct=0.5)
        f = trg.check_profit_target_pct(p, unrealized_pnl=60.0, profit_capture_denominator=None)
        assert f is not None and f.target_state == S.DATA_INSUFFICIENT and f.mandatory

    def test_non_positive_denominator_is_data_insufficient(self):
        p = _policy(profit_target_pct=0.5)
        f = trg.check_profit_target_pct(p, unrealized_pnl=60.0, profit_capture_denominator=0.0)
        assert f is not None and f.target_state == S.DATA_INSUFFICIENT


class TestProfitTargetUnderlyingPrice:
    def test_bullish_target_reached(self):
        p = _policy(profit_target_underlying_price=110.0)
        f = trg.check_profit_target_underlying_price(p, entry_underlying_price=100.0, underlying_price=112.0)
        assert f is not None and f.target_state == S.PROFIT_TARGET_REACHED

    def test_bearish_target_reached(self):
        p = _policy(profit_target_underlying_price=90.0)
        f = trg.check_profit_target_underlying_price(p, entry_underlying_price=100.0, underlying_price=88.0)
        assert f is not None

    def test_missing_data_is_data_insufficient(self):
        p = _policy(profit_target_underlying_price=110.0)
        f = trg.check_profit_target_underlying_price(p, entry_underlying_price=None, underlying_price=112.0)
        assert f.target_state == S.DATA_INSUFFICIENT


class TestProfitTargetOptionValue:
    def test_fires_when_appreciating_target_reached(self):
        p = _policy(profit_target_option_value=5.0)
        f = trg.check_profit_target_option_value(p, entry_option_value=2.0, current_option_value=6.0)
        assert f is not None and f.target_state == S.PROFIT_TARGET_REACHED


# --------------------------------------------------------------- Part 5: loss

class TestMaxLossPct:
    def test_fires_at_loss_limit(self):
        p = _policy(max_loss_pct=0.5)
        f = trg.check_max_loss_pct(p, unrealized_pnl=-600.0, max_loss_dollars=1000.0)
        assert f is not None and f.target_state == S.LOSS_THRESHOLD_REACHED and f.mandatory and f.category == "hard_loss_exposure"

    def test_does_not_fire_above_limit(self):
        p = _policy(max_loss_pct=0.5)
        assert trg.check_max_loss_pct(p, unrealized_pnl=-100.0, max_loss_dollars=1000.0) is None

    def test_missing_max_loss_dollars_is_data_insufficient(self):
        p = _policy(max_loss_pct=0.5)
        f = trg.check_max_loss_pct(p, unrealized_pnl=-600.0, max_loss_dollars=None)
        assert f.target_state == S.DATA_INSUFFICIENT


class TestMaxLossMultipleOfCredit:
    def test_fires_at_multiple(self):
        p = _policy(max_loss_multiple_of_credit=1.5)
        f = trg.check_max_loss_multiple_of_credit(p, unrealized_pnl=-200.0, initial_credit=100.0)
        assert f is not None and f.mandatory

    def test_missing_credit_is_data_insufficient(self):
        p = _policy(max_loss_multiple_of_credit=1.5)
        f = trg.check_max_loss_multiple_of_credit(p, unrealized_pnl=-200.0, initial_credit=None)
        assert f.target_state == S.DATA_INSUFFICIENT

    def test_non_positive_credit_is_data_insufficient(self):
        p = _policy(max_loss_multiple_of_credit=1.5)
        f = trg.check_max_loss_multiple_of_credit(p, unrealized_pnl=-200.0, initial_credit=0.0)
        assert f.target_state == S.DATA_INSUFFICIENT


class TestUnderlyingTechnicalInvalidation:
    def test_bearish_invalidation_fires_below(self):
        p = _policy(underlying_technical_invalidation_price=90.0)
        f = trg.check_underlying_technical_invalidation(p, entry_underlying_price=100.0, underlying_price=88.0)
        assert f is not None and f.mandatory and f.category == "hard_loss_exposure"

    def test_bullish_invalidation_fires_above(self):
        p = _policy(underlying_technical_invalidation_price=110.0)
        f = trg.check_underlying_technical_invalidation(p, entry_underlying_price=100.0, underlying_price=112.0)
        assert f is not None


# ---------------------------------------------------------------- Part 6: DTE

class TestDte:
    def test_forced_exit_outranks_management_review(self):
        p = _policy(management_dte=28, forced_exit_dte=21)
        fs = trg.check_dte(p, dte=15)
        assert len(fs) == 1 and fs[0].trigger_name == "forced_exit_dte" and fs[0].mandatory

    def test_management_review_alone(self):
        p = _policy(management_dte=28, forced_exit_dte=21)
        fs = trg.check_dte(p, dte=25)
        assert len(fs) == 1 and fs[0].trigger_name == "management_dte" and not fs[0].mandatory

    def test_no_trigger_far_from_expiration(self):
        p = _policy(management_dte=28, forced_exit_dte=21)
        assert trg.check_dte(p, dte=40) == []

    def test_missing_dte_when_configured_is_data_insufficient(self):
        p = _policy(forced_exit_dte=21)
        fs = trg.check_dte(p, dte=None)
        assert len(fs) == 1 and fs[0].target_state == S.DATA_INSUFFICIENT

    def test_missing_dte_when_unconfigured_returns_nothing(self):
        assert trg.check_dte(_policy(), dte=None) == []


# -------------------------------------------------------------- Part 7: delta

class TestDelta:
    def test_close_threshold_outranks_review(self):
        p = _policy(delta_threshold=0.30, delta_close_threshold=0.50)
        f = trg.check_delta(p, position_delta_abs=0.55)
        assert f.trigger_name == "delta_close_threshold" and f.mandatory

    def test_review_threshold_alone(self):
        p = _policy(delta_threshold=0.30, delta_close_threshold=0.50)
        f = trg.check_delta(p, position_delta_abs=0.35)
        assert f.trigger_name == "delta_threshold" and not f.mandatory

    def test_missing_delta_is_data_insufficient(self):
        p = _policy(delta_threshold=0.30)
        f = trg.check_delta(p, position_delta_abs=None)
        assert f.target_state == S.DATA_INSUFFICIENT

    def test_not_configured_returns_none(self):
        assert trg.check_delta(_policy(), position_delta_abs=0.9) is None


# --------------------------------------------------------- Part 8: volatility

class TestVolatility:
    def test_iv_contraction_capture_fires_below_threshold(self):
        p = _policy(volatility_trigger="iv_contraction_capture", iv_percentile_trigger=0.2)
        f = trg.check_volatility(p, iv_percentile=0.1)
        assert f is not None and f.target_state == S.VOLATILITY_TRIGGERED

    def test_iv_expansion_review_fires_above_threshold(self):
        p = _policy(volatility_trigger="iv_expansion_review", iv_percentile_trigger=0.7)
        f = trg.check_volatility(p, iv_percentile=0.8)
        assert f is not None

    def test_no_fire_when_not_crossed(self):
        p = _policy(volatility_trigger="iv_contraction_capture", iv_percentile_trigger=0.2)
        assert trg.check_volatility(p, iv_percentile=0.5) is None

    def test_missing_iv_percentile_is_data_insufficient(self):
        p = _policy(volatility_trigger="iv_contraction_capture", iv_percentile_trigger=0.2)
        f = trg.check_volatility(p, iv_percentile=None)
        assert f.target_state == S.DATA_INSUFFICIENT


# ------------------------------------------------------------ Part 9: regime

class TestRegimeChange:
    def test_exit_action_is_mandatory(self):
        p = _policy(regime_change_action="exit")
        f = trg.check_regime_change(p, entry_regime="low_vol", current_regime="crisis")
        assert f is not None and f.mandatory and f.target_state == S.REGIME_CHANGE_TRIGGERED

    def test_review_action_is_not_mandatory(self):
        p = _policy(regime_change_action="review")
        f = trg.check_regime_change(p, entry_regime="low_vol", current_regime="crisis")
        assert f is not None and not f.mandatory

    def test_no_action_never_fires(self):
        p = _policy(regime_change_action="no_action")
        assert trg.check_regime_change(p, entry_regime="low_vol", current_regime="crisis") is None

    def test_no_change_no_fire(self):
        p = _policy(regime_change_action="exit")
        assert trg.check_regime_change(p, entry_regime="low_vol", current_regime="low_vol") is None

    def test_missing_regime_data_is_data_insufficient(self):
        p = _policy(regime_change_action="exit")
        f = trg.check_regime_change(p, entry_regime="low_vol", current_regime=None)
        assert f.target_state == S.DATA_INSUFFICIENT


# ----------------------------------------------------- Part 10: earnings/events

class TestEarnings:
    def test_missing_data_never_interpreted_as_no_earnings(self):
        p = _policy(earnings_exit_days=5)
        f = trg.check_earnings(p, earnings_data_available=False, days_to_earnings=None)
        assert f is not None and f.target_state == S.DATA_INSUFFICIENT

    def test_fires_inside_window(self):
        p = _policy(earnings_exit_days=5)
        f = trg.check_earnings(p, earnings_data_available=True, days_to_earnings=3)
        assert f is not None and f.mandatory and f.category == "event_risk"

    def test_no_fire_outside_window(self):
        p = _policy(earnings_exit_days=5)
        assert trg.check_earnings(p, earnings_data_available=True, days_to_earnings=30) is None

    def test_exposure_permitted_suppresses_rule_entirely(self):
        p = _policy(earnings_exit_days=5, earnings_exposure_permitted=True)
        assert trg.check_earnings(p, earnings_data_available=False, days_to_earnings=None) is None

    def test_not_configured_returns_none(self):
        assert trg.check_earnings(_policy(), earnings_data_available=False, days_to_earnings=None) is None


# --------------------------------------------------------- Part 11: liquidity

class TestLiquidity:
    def test_spread_deterioration_fires(self):
        p = _policy(liquidity_deterioration_threshold=0.15)
        fs = trg.check_liquidity(p, spread_pct=0.3, quote_age_minutes=None)
        assert len(fs) == 1 and fs[0].category == "liquidity_risk" and not fs[0].mandatory

    def test_stale_quote_is_data_insufficient(self):
        p = _policy(quote_staleness_limit_minutes=10)
        fs = trg.check_liquidity(p, spread_pct=None, quote_age_minutes=20)
        assert len(fs) == 1 and fs[0].target_state == S.DATA_INSUFFICIENT

    def test_both_rules_can_fire_together(self):
        p = _policy(liquidity_deterioration_threshold=0.15, quote_staleness_limit_minutes=10)
        fs = trg.check_liquidity(p, spread_pct=0.3, quote_age_minutes=20)
        assert len(fs) == 2

    def test_neither_configured_returns_nothing(self):
        assert trg.check_liquidity(_policy(), spread_pct=0.9, quote_age_minutes=999) == []


# ------------------------------------------------------- Part 12: assignment

class TestAssignmentRisk:
    def test_otm_never_fires(self):
        p = _policy(assignment_risk_rule="review")
        assert trg.check_assignment_risk(p, short_leg_is_itm=False, short_leg_extrinsic_value=None, dte=5) is None

    def test_itm_at_expiration_is_mandatory(self):
        p = _policy(assignment_risk_rule="review")
        f = trg.check_assignment_risk(p, short_leg_is_itm=True, short_leg_extrinsic_value=None, dte=0)
        assert f is not None and f.mandatory and f.category == "assignment_expiration"

    def test_itm_low_extrinsic_is_advisory(self):
        p = _policy(assignment_risk_rule="review")
        f = trg.check_assignment_risk(p, short_leg_is_itm=True, short_leg_extrinsic_value=0.02, dte=10)
        assert f is not None and not f.mandatory

    def test_itm_high_extrinsic_does_not_fire(self):
        p = _policy(assignment_risk_rule="review")
        assert trg.check_assignment_risk(p, short_leg_is_itm=True, short_leg_extrinsic_value=2.0, dte=10) is None

    def test_missing_itm_status_is_data_insufficient(self):
        p = _policy(assignment_risk_rule="review")
        f = trg.check_assignment_risk(p, short_leg_is_itm=None, short_leg_extrinsic_value=None, dte=10)
        assert f.target_state == S.DATA_INSUFFICIENT

    def test_not_configured_returns_none(self):
        assert trg.check_assignment_risk(_policy(), short_leg_is_itm=True, short_leg_extrinsic_value=0.0, dte=0) is None


class TestPrecedenceOrder:
    def test_precedence_order_matches_part_18(self):
        assert trg.PRECEDENCE_ORDER == (
            "system_data_safety", "risk_halt", "hard_loss_exposure", "assignment_expiration",
            "event_risk", "liquidity_risk", "time_exit", "profit_target", "delta_volatility_review",
            "optional_adjustment", "hold",
        )
