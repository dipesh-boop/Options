"""Tests for the promotion gate — the single choke point a hypothesis
must pass through to become a production strategy version. All four
conditions (validation, out-of-sample, Risk Engine PASS, human approval)
are independently required; each test below removes exactly one and
confirms promotion is refused."""
from __future__ import annotations

import pytest

from src.research.promotion import PromotionError, PromotionRequest, promote_strategy
from src.research.risk_review import StrategyRiskReview


def _pass_review(**overrides) -> StrategyRiskReview:
    base = dict(verdict="PASS", reasons=(), max_drawdown_pct=0.02, var_95=0.03)
    base.update(overrides)
    return StrategyRiskReview(**base)


def _valid_request(**overrides) -> PromotionRequest:
    base = dict(
        hypothesis_id="hyp-1",
        strategy_version="pcs-v2",
        validation_passed=True,
        out_of_sample_passed=True,
        risk_review=_pass_review(),
        human_approved=True,
        human_approver="dipesh",
    )
    base.update(overrides)
    return PromotionRequest(**base)


class TestAllFourGatesRequired:
    def test_happy_path_promotes(self):
        record = promote_strategy(_valid_request())
        assert record.hypothesis_id == "hyp-1"
        assert record.strategy_version == "pcs-v2"
        assert record.human_approver == "dipesh"
        assert record.promoted_at.tzinfo is not None

    def test_missing_validation_blocked(self):
        with pytest.raises(PromotionError, match="validation not passed"):
            promote_strategy(_valid_request(validation_passed=False))

    def test_missing_out_of_sample_blocked(self):
        with pytest.raises(PromotionError, match="out-of-sample test not passed"):
            promote_strategy(_valid_request(out_of_sample_passed=False))

    def test_risk_review_concern_blocked(self):
        with pytest.raises(PromotionError, match="risk review verdict"):
            promote_strategy(_valid_request(risk_review=_pass_review(verdict="CONCERN")))

    def test_risk_review_reject_blocked(self):
        with pytest.raises(PromotionError, match="risk review verdict"):
            promote_strategy(_valid_request(risk_review=_pass_review(verdict="REJECT")))

    def test_missing_human_approval_blocked(self):
        with pytest.raises(PromotionError, match="human approval missing"):
            promote_strategy(_valid_request(human_approved=False, human_approver=None))

    def test_human_approved_true_but_no_approver_name_still_blocked(self):
        """`human_approved=True` alone is not enough -- an approver must
        actually be named, so a promotion can never be traced to "nobody
        in particular.\""""
        with pytest.raises(PromotionError, match="human approval missing"):
            promote_strategy(_valid_request(human_approved=True, human_approver=None))

    def test_human_approver_named_but_flag_false_still_blocked(self):
        with pytest.raises(PromotionError, match="human approval missing"):
            promote_strategy(_valid_request(human_approved=False, human_approver="dipesh"))

    def test_multiple_missing_gates_all_listed(self):
        with pytest.raises(PromotionError) as exc_info:
            promote_strategy(_valid_request(validation_passed=False, out_of_sample_passed=False, human_approved=False, human_approver=None))
        message = str(exc_info.value)
        assert "validation not passed" in message
        assert "out-of-sample test not passed" in message
        assert "human approval missing" in message

    def test_a_pass_risk_review_alone_does_not_imply_the_other_gates(self):
        """A PASS risk review must never be treated as evidence that
        validation or out-of-sample testing also succeeded -- the four
        gates are independent, never inferred from one another."""
        with pytest.raises(PromotionError):
            promote_strategy(_valid_request(validation_passed=False, risk_review=_pass_review()))
