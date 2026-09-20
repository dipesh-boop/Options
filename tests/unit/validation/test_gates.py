from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.validation.gates import evaluate_90_day_gate, evaluate_checkpoint
from src.validation.protocol import SampleSizeStatus, ValidationPeriod
from src.validation.scorecard import ReturnInputs, RiskInputs, build_scorecard

from .conftest import _limits
from .test_scorecard import _scorecard


class TestEvaluateCheckpoint:
    def test_rejects_invalid_day(self, limits):
        with pytest.raises(ValueError):
            evaluate_checkpoint(day=45, current_drawdown_pct=0.0, rule_violations=0, sharpe=1.0, limits=limits)

    def test_clean_run_continues(self, limits):
        result = evaluate_checkpoint(day=30, current_drawdown_pct=0.02, rule_violations=0, sharpe=1.2, limits=limits)
        assert result == "CONTINUE"

    def test_drawdown_at_warning_continues_with_warning(self, limits):
        result = evaluate_checkpoint(day=30, current_drawdown_pct=0.09, rule_violations=0, sharpe=1.0, limits=limits)
        assert result == "CONTINUE_WITH_WARNING"

    def test_negative_sharpe_continues_with_warning(self, limits):
        result = evaluate_checkpoint(day=60, current_drawdown_pct=0.0, rule_violations=0, sharpe=-0.5, limits=limits)
        assert result == "CONTINUE_WITH_WARNING"

    def test_drawdown_at_halt_threshold_halts(self, limits):
        result = evaluate_checkpoint(day=30, current_drawdown_pct=0.15, rule_violations=0, sharpe=1.5, limits=limits)
        assert result == "HALT_FOR_INVESTIGATION"

    def test_any_rule_violation_halts_regardless_of_everything_else(self, limits):
        """The core rule-violation scenario at the checkpoint level."""
        result = evaluate_checkpoint(day=60, current_drawdown_pct=0.0, rule_violations=1, sharpe=3.0, limits=limits)
        assert result == "HALT_FOR_INVESTIGATION"

    def test_checkpoint_never_returns_a_pass_shaped_value(self, limits):
        """Structural guarantee: CheckpointClassification simply has no
        PASS-shaped value to return in the first place."""
        from src.validation.gates import CheckpointClassification

        assert CheckpointClassification.__args__ == ("CONTINUE", "CONTINUE_WITH_WARNING", "HALT_FOR_INVESTIGATION")


class TestEvaluate90DayGate:
    def test_never_evaluates_before_day_90(self, limits):
        """The core "never PASS before day 90" scenario, enforced
        structurally: the function refuses to run at all."""
        scorecard = _scorecard()
        with pytest.raises(ValueError, match="before day 90"):
            evaluate_90_day_gate(
                day=60, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
                rule_violations=0, current_drawdown_pct=0.0, limits=limits,
            )

    def test_insufficient_sample_always_extends_regardless_of_performance(self, limits):
        """The core insufficient-sample scenario: even excellent
        performance (high Sharpe, strong return) cannot PASS below the
        minimum trade count."""
        scorecard = _scorecard(sample_status=SampleSizeStatus.INSUFFICIENT_SAMPLE, sharpe=3.0, since_inception_return=0.25)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.INSUFFICIENT_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.0, limits=limits,
        )
        assert result == "EXTEND_VALIDATION"

    def test_rule_violation_halts_even_with_preferred_sample_and_good_performance(self, limits):
        scorecard = _scorecard(sample_status=SampleSizeStatus.PREFERRED_SAMPLE, sharpe=2.0, since_inception_return=0.20)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
            rule_violations=1, current_drawdown_pct=0.0, limits=limits,
        )
        assert result == "HALT"

    def test_drawdown_at_halt_threshold_halts(self, limits):
        scorecard = _scorecard(sample_status=SampleSizeStatus.PREFERRED_SAMPLE)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.16, limits=limits,
        )
        assert result == "HALT"

    def test_negative_expectancy_with_a_loss_fails_research_review(self, limits):
        scorecard = _scorecard(sample_status=SampleSizeStatus.PREFERRED_SAMPLE, sharpe=-0.5, since_inception_return=-0.05)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.05, limits=limits,
        )
        assert result == "FAIL_RESEARCH_REVIEW"

    def test_minimum_sample_clean_run_is_conditional_pass(self, limits):
        scorecard = _scorecard(sample_status=SampleSizeStatus.MINIMUM_SAMPLE, sharpe=1.2, since_inception_return=0.06)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.MINIMUM_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.03, limits=limits,
        )
        assert result == "CONDITIONAL_PASS"

    def test_preferred_sample_clean_run_passes_for_extended_validation(self, limits):
        scorecard = _scorecard(sample_status=SampleSizeStatus.PREFERRED_SAMPLE, sharpe=1.5, since_inception_return=0.08)
        result = evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.02, limits=limits,
        )
        assert result == "PASS_FOR_EXTENDED_VALIDATION"

    def test_gate_never_returns_a_live_trading_authorization(self, limits):
        """Structural guarantee: GateClassification has no LIVE-shaped
        value at all."""
        from src.validation.gates import GateClassification

        for value in GateClassification.__args__:
            assert "LIVE" not in value.upper()
            assert "EXECUTE" not in value.upper()

    def test_day_90_is_the_earliest_allowed_day(self, limits):
        scorecard = _scorecard()
        # day == 90 must work (not just day > 90)
        evaluate_90_day_gate(
            day=90, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, scorecard=scorecard,
            rule_violations=0, current_drawdown_pct=0.0, limits=limits,
        )
