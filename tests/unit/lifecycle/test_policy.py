"""Part 3: `ManagementPolicy` structural validation -- every field
requires the underlying `StrategyKind` to actually have the structural
property that field presupposes."""
from __future__ import annotations

import pytest

from src.lifecycle.policy import ManagementPolicy, UnsupportedPolicyFieldError, InvalidPolicyError, validate_policy_for_strategy
from src.strategies.base import StrategyKind


def _policy(**overrides) -> ManagementPolicy:
    base = dict(name="TEST", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, description="test")
    base.update(overrides)
    return ManagementPolicy(**base)


class TestFrozenAndOptionalFields:
    def test_every_field_besides_identity_defaults_to_none_or_false(self):
        p = _policy()
        assert p.profit_target_pct is None
        assert p.roll_allowed is False
        assert p.earnings_exposure_permitted is False

    def test_policy_is_frozen(self):
        p = _policy()
        with pytest.raises(Exception):
            p.profit_target_pct = 0.5  # type: ignore[misc]


class TestStructuralValidation:
    def test_delta_threshold_requires_short_leg(self):
        p = _policy(strategy_kind=StrategyKind.BULL_CALL_SPREAD, delta_threshold=0.3)
        with pytest.raises(UnsupportedPolicyFieldError):
            validate_policy_for_strategy(p)

    def test_delta_close_threshold_requires_short_leg(self):
        p = _policy(strategy_kind=StrategyKind.LONG_STRADDLE, delta_close_threshold=0.4)
        with pytest.raises(UnsupportedPolicyFieldError):
            validate_policy_for_strategy(p)

    def test_max_loss_multiple_of_credit_requires_credit_at_entry(self):
        p = _policy(strategy_kind=StrategyKind.LONG_CALL, max_loss_multiple_of_credit=1.5)
        with pytest.raises(UnsupportedPolicyFieldError):
            validate_policy_for_strategy(p)

    def test_assignment_risk_rule_requires_assignable_strategy(self):
        p = _policy(strategy_kind=StrategyKind.LONG_STRADDLE, assignment_risk_rule="review")
        with pytest.raises(UnsupportedPolicyFieldError):
            validate_policy_for_strategy(p)

    def test_early_assignment_rule_requires_assignable_strategy(self):
        p = _policy(strategy_kind=StrategyKind.PROTECTIVE_PUT, early_assignment_rule="review")
        with pytest.raises(UnsupportedPolicyFieldError):
            validate_policy_for_strategy(p)

    def test_valid_combination_on_put_credit_spread_passes(self):
        p = _policy(
            delta_threshold=0.3, delta_close_threshold=0.45, max_loss_multiple_of_credit=1.5,
            assignment_risk_rule="review", early_assignment_rule="review",
        )
        validate_policy_for_strategy(p)  # does not raise

    def test_valid_combination_on_wheel_passes(self):
        p = _policy(
            strategy_kind=StrategyKind.WHEEL, delta_threshold=0.3, assignment_risk_rule="review",
            max_loss_multiple_of_credit=2.0,
        )
        validate_policy_for_strategy(p)


class TestNumericRangeValidation:
    def test_profit_target_pct_out_of_range(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(profit_target_pct=1.5))

    def test_profit_target_pct_zero_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(profit_target_pct=0.0))

    def test_max_loss_pct_out_of_range(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(max_loss_pct=1.5))

    def test_max_loss_multiple_of_credit_must_be_positive(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(max_loss_multiple_of_credit=-1.0))

    def test_negative_management_dte_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(management_dte=-1))

    def test_negative_forced_exit_dte_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(forced_exit_dte=-1))

    def test_forced_exit_dte_after_management_dte_rejected(self):
        """Forced exit must come at or after the mandatory review point
        (a SMALLER or equal DTE, never a larger one)."""
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(management_dte=21, forced_exit_dte=28))

    def test_forced_exit_dte_equal_to_management_dte_allowed(self):
        validate_policy_for_strategy(_policy(management_dte=21, forced_exit_dte=21))

    def test_delta_threshold_out_of_range(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(delta_threshold=1.5))

    def test_delta_close_threshold_smaller_than_delta_threshold_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(delta_threshold=0.5, delta_close_threshold=0.3))

    def test_delta_close_threshold_equal_to_delta_threshold_allowed(self):
        validate_policy_for_strategy(_policy(delta_threshold=0.4, delta_close_threshold=0.4))

    def test_invalid_regime_change_action_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(regime_change_action="panic"))

    def test_negative_earnings_exit_days_rejected(self):
        with pytest.raises(InvalidPolicyError):
            validate_policy_for_strategy(_policy(earnings_exit_days=-1))
