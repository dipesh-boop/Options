"""Schema-level tests for DevilsAdvocateReview and its sub-models
(Step 11)."""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.llm.schemas import DevilsAdvocateReview, ensure_devils_advocate_review
from tests.unit.llm.conftest import (
    valid_devils_advocate_review_input,
    valid_failure_scenarios,
    valid_fidelity_execution_risk,
    valid_risk_assessment,
)


class TestValidConstruction:
    @pytest.mark.parametrize("verdict", ["PASS", "CAUTION", "REJECT", "REPRICE_REQUIRED"])
    def test_every_valid_verdict_constructs(self, verdict: str):
        overrides = {"verdict": verdict}
        if verdict in ("REPRICE_REQUIRED",):
            overrides["fidelity_execution_risk"] = valid_fidelity_execution_risk(reprice_required=True)
        review = DevilsAdvocateReview(**valid_devils_advocate_review_input(**overrides))
        assert review.verdict == verdict


class TestNoNumericFieldsExistAtAll:
    def test_every_field_is_not_numeric(self):
        for name, field in DevilsAdvocateReview.model_fields.items():
            assert field.annotation not in (float, int, "float", "int"), f"field {name!r} has a numeric type"

    def test_failure_scenario_has_no_numeric_fields(self):
        from src.llm.schemas import FailureScenario

        for name, field in FailureScenario.model_fields.items():
            assert field.annotation not in (float, int, "float", "int"), f"field {name!r} has a numeric type"


class TestApproveIsNotAValidVerdict:
    def test_approve_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(verdict="approve"))

    def test_execute_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(verdict="execute"))


class TestAll18CategoriesRequired:
    def test_missing_one_category_rejected(self):
        incomplete = valid_risk_assessment()[:-1]  # drop one
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(risk_assessment=incomplete))

    def test_duplicate_category_rejected(self):
        assessment = valid_risk_assessment()
        duplicated = assessment[:-1] + [assessment[0]]  # replace last with a duplicate of the first
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(risk_assessment=duplicated))

    def test_unrecognized_category_rejected(self):
        assessment = valid_risk_assessment()[:-1] + [
            {"category": "not_a_real_category", "applicable": False, "note": "n/a"}
        ]
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(risk_assessment=assessment))

    def test_exactly_18_categories_accepted(self):
        review = DevilsAdvocateReview(**valid_devils_advocate_review_input())
        assert len(review.risk_assessment) == 18


class TestAtLeastThreeFailureScenarios:
    def test_two_scenarios_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(failure_scenarios=valid_failure_scenarios()[:2]))

    def test_three_scenarios_accepted(self):
        review = DevilsAdvocateReview(**valid_devils_advocate_review_input())
        assert len(review.failure_scenarios) >= 3

    def test_blank_warning_indicator_rejected(self):
        scenarios = valid_failure_scenarios()
        scenarios[0] = {**scenarios[0], "warning_indicators": ["   "]}
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(failure_scenarios=scenarios))

    def test_empty_warning_indicators_rejected(self):
        scenarios = valid_failure_scenarios()
        scenarios[0] = {**scenarios[0], "warning_indicators": []}
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(failure_scenarios=scenarios))


class TestNoFabricatedProbability:
    @pytest.mark.parametrize("bad_probability", ["23%", "0.23", 0.23, "very likely"])
    def test_non_categorical_probability_rejected(self, bad_probability):
        scenarios = valid_failure_scenarios()
        scenarios[0] = {**scenarios[0], "probability_category": bad_probability}
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(failure_scenarios=scenarios))


class TestRepriceRequiredConsistency:
    def test_fidelity_reprice_required_true_with_pass_verdict_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(
                **valid_devils_advocate_review_input(
                    verdict="PASS", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
                )
            )

    def test_fidelity_reprice_required_true_with_caution_verdict_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(
                **valid_devils_advocate_review_input(
                    verdict="CAUTION", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
                )
            )

    def test_fidelity_reprice_required_true_with_reprice_required_verdict_accepted(self):
        review = DevilsAdvocateReview(
            **valid_devils_advocate_review_input(
                verdict="REPRICE_REQUIRED", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
            )
        )
        assert review.verdict == "REPRICE_REQUIRED"

    def test_fidelity_reprice_required_true_with_reject_verdict_accepted(self):
        review = DevilsAdvocateReview(
            **valid_devils_advocate_review_input(
                verdict="REJECT", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
            )
        )
        assert review.verdict == "REJECT"


class TestForbiddenExtraFields:
    @pytest.mark.parametrize(
        "smuggled",
        [
            {"approved_contracts": 100},
            {"probability_of_loss": 0.4},
            {"execute": True},
            {"override_portfolio_manager": True},
            {"account_balance": 50_000.0},
        ],
    )
    def test_smuggled_field_rejected(self, smuggled: dict):
        payload = {**valid_devils_advocate_review_input(), **smuggled}
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**payload)


class TestRequiredNarrativeFields:
    def test_missing_why_not_thesis_rejected(self):
        payload = {k: v for k, v in valid_devils_advocate_review_input().items() if k != "why_not_thesis"}
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**payload)

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            DevilsAdvocateReview(**valid_devils_advocate_review_input(timestamp=datetime(2026, 9, 20, 14, 0).isoformat()))


class TestEnsureDevilsAdvocateReviewBoundaryGuard:
    def test_guard_accepts_a_real_instance(self):
        review = DevilsAdvocateReview(**valid_devils_advocate_review_input())
        assert ensure_devils_advocate_review(review) is review

    def test_guard_rejects_a_plain_dict(self):
        with pytest.raises(TypeError):
            ensure_devils_advocate_review(valid_devils_advocate_review_input())

    def test_guard_rejects_a_subclass(self):
        class SneakyDevilsAdvocateReview(DevilsAdvocateReview):
            pass

        sneaky = SneakyDevilsAdvocateReview(**valid_devils_advocate_review_input())
        with pytest.raises(TypeError):
            ensure_devils_advocate_review(sneaky)
