"""Tests for the deterministic Fidelity operational-practicality
classification: LOW/MEDIUM/HIGH burden scoring plus the hard
INCOMPATIBLE rejection for high-frequency/sub-second/constant-intraday
strategies."""
from __future__ import annotations

import pytest

from src.research.fidelity_practicality import (
    FidelityPracticalityInputs,
    classify_fidelity_practicality,
    compute_burden_score,
    is_hard_incompatible,
)


def _inputs(**overrides) -> FidelityPracticalityInputs:
    base = dict(
        trades_per_week=1.0,
        adjustments_per_week=0.5,
        num_legs=2,
        rolling_frequency_per_month=1.0,
        liquidity="high",
        monitoring_requirement="low",
        assignment_complexity="low",
        time_sensitivity="low",
    )
    base.update(overrides)
    return FidelityPracticalityInputs(**base)


class TestValidation:
    def test_negative_frequency_rejected(self):
        with pytest.raises(ValueError):
            _inputs(trades_per_week=-1.0)

    def test_zero_legs_rejected(self):
        with pytest.raises(ValueError):
            _inputs(num_legs=0)


class TestHardIncompatibility:
    def test_subsecond_decisions_forces_incompatible(self):
        result = classify_fidelity_practicality(_inputs(requires_subsecond_decisions=True))
        assert result.rating == "INCOMPATIBLE"
        assert result.hard_rejected is True
        assert "sub-second" in result.reasons[0]

    def test_constant_intraday_adjustment_forces_incompatible(self):
        result = classify_fidelity_practicality(_inputs(requires_constant_intraday_adjustment=True))
        assert result.rating == "INCOMPATIBLE"

    def test_high_frequency_execution_forces_incompatible(self):
        result = classify_fidelity_practicality(_inputs(requires_high_frequency_execution=True))
        assert result.rating == "INCOMPATIBLE"

    def test_incompatible_even_with_otherwise_perfect_low_burden_inputs(self):
        """A strategy that's operationally trivial in every other respect
        is still rejected if it requires sub-second decisions -- the
        hard gate is independent of the weighted score."""
        result = classify_fidelity_practicality(_inputs(
            trades_per_week=0.1, adjustments_per_week=0.0, num_legs=1, rolling_frequency_per_month=0.0,
            liquidity="high", monitoring_requirement="low", assignment_complexity="low", time_sensitivity="low",
            requires_subsecond_decisions=True,
        ))
        assert result.rating == "INCOMPATIBLE"

    def test_is_hard_incompatible_helper(self):
        assert is_hard_incompatible(_inputs(requires_high_frequency_execution=True)) is True
        assert is_hard_incompatible(_inputs()) is False


class TestBurdenScoring:
    def test_minimal_inputs_score_low(self):
        result = classify_fidelity_practicality(_inputs())
        assert result.rating == "LOW"
        assert result.hard_rejected is False

    def test_high_trade_frequency_increases_score(self):
        low_score = compute_burden_score(_inputs(trades_per_week=1.0))
        high_score = compute_burden_score(_inputs(trades_per_week=15.0))
        assert high_score > low_score

    def test_thin_liquidity_increases_score(self):
        deep = compute_burden_score(_inputs(liquidity="high"))
        thin = compute_burden_score(_inputs(liquidity="low"))
        assert thin > deep

    def test_more_legs_increases_score(self):
        two_legs = compute_burden_score(_inputs(num_legs=2))
        four_legs = compute_burden_score(_inputs(num_legs=4))
        assert four_legs > two_legs

    def test_everything_maxed_out_rates_high(self):
        result = classify_fidelity_practicality(_inputs(
            trades_per_week=20.0, adjustments_per_week=10.0, num_legs=4, rolling_frequency_per_month=10.0,
            liquidity="low", monitoring_requirement="high", assignment_complexity="high", time_sensitivity="high",
        ))
        assert result.rating == "HIGH"
        assert result.hard_rejected is False

    def test_moderate_inputs_rate_medium(self):
        result = classify_fidelity_practicality(_inputs(
            trades_per_week=6.0, adjustments_per_week=1.0, num_legs=2, rolling_frequency_per_month=1.0,
            liquidity="medium", monitoring_requirement="medium", assignment_complexity="low", time_sensitivity="low",
        ))
        assert result.rating == "MEDIUM"

    def test_reasons_always_populated_for_a_non_hard_rejected_result(self):
        result = classify_fidelity_practicality(_inputs())
        assert len(result.reasons) >= 1
