"""Tests for the DECISION QUALITY classifier: "Do NOT judge decision
quality solely by P&L" -- the classifier must not even be able to."""
from __future__ import annotations

import pytest

from src.risk.reason_codes import RiskDecision
from src.workflows.decision_quality import (
    DecisionQualityInputs,
    classify_decision_quality,
    summarize_decision_quality,
    was_good_decision,
    was_good_outcome,
)


def _inputs(**overrides) -> DecisionQualityInputs:
    base = dict(risk_decision=RiskDecision.APPROVE, devils_advocate_verdict="PASS", probability_of_profit=0.65, realistic_pnl=100.0)
    base.update(overrides)
    return DecisionQualityInputs(**base)


class TestGoodDecisionGoodOutcome:
    def test_approved_pass_high_probability_and_profit_is_this_quadrant(self):
        result = classify_decision_quality(_inputs())
        assert result.label == "good_decision_good_outcome"
        assert result.good_decision is True
        assert result.good_outcome is True


class TestGoodDecisionBadOutcome:
    def test_sound_process_but_a_loss_is_still_a_good_decision(self):
        """The core guarantee: a loss alone must never flip
        `good_decision` to False."""
        result = classify_decision_quality(_inputs(realistic_pnl=-50.0))
        assert result.label == "good_decision_bad_outcome"
        assert result.good_decision is True
        assert result.good_outcome is False


class TestBadDecisionGoodOutcome:
    def test_risk_rejected_trade_that_would_have_won_is_still_a_bad_decision(self):
        """The mirror guarantee: a win alone must never flip
        `good_decision` to True."""
        result = classify_decision_quality(_inputs(risk_decision=RiskDecision.REJECT, realistic_pnl=200.0))
        assert result.label == "bad_decision_good_outcome"
        assert result.good_decision is False
        assert result.good_outcome is True

    def test_devils_advocate_reject_makes_it_a_bad_decision_regardless_of_risk_engine(self):
        result = classify_decision_quality(_inputs(devils_advocate_verdict="REJECT", realistic_pnl=200.0))
        assert result.good_decision is False

    def test_low_probability_of_profit_makes_it_a_bad_decision(self):
        result = classify_decision_quality(_inputs(probability_of_profit=0.2, realistic_pnl=200.0))
        assert result.good_decision is False


class TestBadDecisionBadOutcome:
    def test_halted_trade_that_lost_money(self):
        result = classify_decision_quality(_inputs(risk_decision=RiskDecision.HALT, realistic_pnl=-50.0))
        assert result.label == "bad_decision_bad_outcome"


class TestResizeCountsAsAnAcceptableRiskDecision:
    def test_resize_is_not_automatically_a_bad_decision(self):
        result = classify_decision_quality(_inputs(risk_decision=RiskDecision.RESIZE))
        assert result.good_decision is True


class TestCautionVerdictIsAcceptable:
    def test_caution_does_not_disqualify_a_good_decision(self):
        result = classify_decision_quality(_inputs(devils_advocate_verdict="CAUTION"))
        assert result.good_decision is True


class TestConfigurableThreshold:
    def test_custom_min_probability_threshold(self):
        inputs = _inputs(probability_of_profit=0.55)
        assert was_good_decision(inputs, min_probability_of_profit=0.5) is True
        assert was_good_decision(inputs, min_probability_of_profit=0.6) is False


class TestHelperFunctionsAgreeWithClassifyResult:
    def test_was_good_decision_and_was_good_outcome_match_classify(self):
        inputs = _inputs(realistic_pnl=-25.0)
        result = classify_decision_quality(inputs)
        assert result.good_decision == was_good_decision(inputs)
        assert result.good_outcome == was_good_outcome(inputs)


class TestSummarizeDecisionQuality:
    def test_counts_and_fractions_across_all_four_quadrants(self):
        results = [
            classify_decision_quality(_inputs(realistic_pnl=100.0)),
            classify_decision_quality(_inputs(realistic_pnl=-50.0)),
            classify_decision_quality(_inputs(risk_decision=RiskDecision.REJECT, realistic_pnl=200.0)),
            classify_decision_quality(_inputs(risk_decision=RiskDecision.HALT, realistic_pnl=-10.0)),
        ]
        summary = summarize_decision_quality(results)
        assert summary.total == 4
        assert summary.counts["good_decision_good_outcome"] == 1
        assert summary.counts["good_decision_bad_outcome"] == 1
        assert summary.counts["bad_decision_good_outcome"] == 1
        assert summary.counts["bad_decision_bad_outcome"] == 1
        assert summary.fraction("good_decision_good_outcome") == pytest.approx(0.25)

    def test_empty_results_all_zero(self):
        summary = summarize_decision_quality([])
        assert summary.total == 0
        assert summary.fraction("good_decision_good_outcome") == 0.0
