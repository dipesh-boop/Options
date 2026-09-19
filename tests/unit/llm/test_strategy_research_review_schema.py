"""Schema-level tests for StrategyResearchReview (Step 14)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.schemas import AnalysisDimension, StrategyResearchReview, ensure_strategy_research_review
from src.research.performance_breakdown import ALL_ANALYSIS_DIMENSIONS
from tests.unit.llm.conftest import valid_strategy_research_review_input


class TestValidConstruction:
    def test_well_formed_review_constructs(self):
        review = StrategyResearchReview(**valid_strategy_research_review_input())
        assert review.recommendation == "proceed_to_out_of_sample"
        assert review.fidelity_practicality_rating == "LOW"

    def test_ensure_strategy_research_review_accepts_a_real_instance(self):
        review = StrategyResearchReview(**valid_strategy_research_review_input())
        assert ensure_strategy_research_review(review) is review

    def test_ensure_strategy_research_review_rejects_a_plain_dict(self):
        with pytest.raises(TypeError):
            ensure_strategy_research_review(valid_strategy_research_review_input())

    def test_ensure_strategy_research_review_rejects_a_different_schema_instance(self):
        from src.llm.schemas import RiskReviewNote

        other = RiskReviewNote(proposal_id="p", concerns=[], concurs_with_quant_review=True, note="n")
        with pytest.raises(TypeError):
            ensure_strategy_research_review(other)


class TestNoNumericFieldsExistAtAll:
    """The structural guarantee the schema's docstring claims: there is
    no field of numeric type anywhere on StrategyResearchReview -- every
    performance figure must be referenced by hypothesis_id, not restated
    as a fresh number here."""

    def test_every_field_is_str_bool_list_or_datetime(self):
        for name, field in StrategyResearchReview.model_fields.items():
            annotation = field.annotation
            assert annotation not in (float, int, "float", "int"), f"field {name!r} has a numeric type"


class TestExtraFieldsForbidden:
    def test_cannot_smuggle_an_approval_or_promotion_field(self):
        for forbidden in ("human_approved", "promoted", "approved", "authoritative_pnl", "backtest_cagr"):
            with pytest.raises(ValidationError):
                StrategyResearchReview(**valid_strategy_research_review_input(**{forbidden: True}))


class TestSupportingDimensionsValidation:
    def test_only_the_twelve_named_dimensions_accepted(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(supporting_dimensions=["not_a_real_dimension"]))

    def test_duplicate_dimensions_rejected(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(supporting_dimensions=["delta", "delta"]))

    def test_at_least_one_dimension_required(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(supporting_dimensions=[]))

    def test_all_twelve_dimensions_individually_accepted(self):
        for dim in ALL_ANALYSIS_DIMENSIONS:
            review = StrategyResearchReview(**valid_strategy_research_review_input(supporting_dimensions=[dim]))
            assert review.supporting_dimensions == [dim]


class TestAnalysisDimensionLiteralStaysInSyncWithResearchPackage:
    """`src.llm.schemas.AnalysisDimension` deliberately mirrors (does not
    import) `src.research.performance_breakdown.AnalysisDimension` --
    this test is the mechanical drift check that relationship depends on."""

    def test_literal_values_match_exactly(self):
        schema_values = set(AnalysisDimension.__args__)  # type: ignore[attr-defined]
        assert schema_values == set(ALL_ANALYSIS_DIMENSIONS)


class TestIncompatibleRatingConsistency:
    def test_incompatible_rating_with_proceed_recommendation_rejected(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(
                fidelity_practicality_rating="INCOMPATIBLE", recommendation="proceed_to_backtest",
            ))

    def test_incompatible_rating_with_reject_recommendation_allowed(self):
        review = StrategyResearchReview(**valid_strategy_research_review_input(
            fidelity_practicality_rating="INCOMPATIBLE", recommendation="reject_hypothesis",
        ))
        assert review.recommendation == "reject_hypothesis"

    def test_incompatible_rating_with_escalate_recommendation_allowed(self):
        review = StrategyResearchReview(**valid_strategy_research_review_input(
            fidelity_practicality_rating="INCOMPATIBLE", recommendation="escalate_for_human_review",
        ))
        assert review.recommendation == "escalate_for_human_review"

    def test_low_rating_with_proceed_recommendation_allowed(self):
        review = StrategyResearchReview(**valid_strategy_research_review_input(
            fidelity_practicality_rating="LOW", recommendation="proceed_to_backtest",
        ))
        assert review.recommendation == "proceed_to_backtest"


class TestTimestampMustBeTimezoneAware:
    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(timestamp="2026-09-20T12:00:00"))


class TestBlankEntriesRejected:
    def test_blank_overfitting_concern_rejected(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(overfitting_concerns=["   "]))

    def test_blank_risk_identified_rejected(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(risks_identified=["   "]))

    def test_at_least_one_risk_identified_required(self):
        with pytest.raises(ValidationError):
            StrategyResearchReview(**valid_strategy_research_review_input(risks_identified=[]))
