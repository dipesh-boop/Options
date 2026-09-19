"""Orchestration tests for src.llm.strategy_research (Step 14): missing
inputs, cross-check failures, and the Python-overrides-the-LLM
relationship for the Fidelity practicality rating."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.client import LLMClient
from src.llm.strategy_research import (
    MissingInputError,
    StrategyResearchCrossCheckError,
    StrategyResearchInputs,
    evaluate_hypothesis,
    validate_inputs_complete,
)
from tests.unit.llm.conftest import (
    make_fidelity_practicality_context,
    make_overfitting_guard_context,
    make_performance_breakdown_context,
    valid_strategy_research_review_input,
)


def _full_inputs(**overrides) -> StrategyResearchInputs:
    base = dict(
        hypothesis_id="hyp-1",
        hypothesis_statement="15-20 delta PCS outperform 25-30 delta in high IV",
        breakdowns=(make_performance_breakdown_context(),),
        overfitting=make_overfitting_guard_context(),
        fidelity_practicality=make_fidelity_practicality_context(),
    )
    base.update(overrides)
    return StrategyResearchInputs(**base)


def _client_for(payload: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))


class TestMissingInputs:
    @pytest.mark.parametrize("field", ["hypothesis_id", "hypothesis_statement", "breakdowns", "overfitting", "fidelity_practicality"])
    def test_each_required_input_missing_raises(self, field):
        kwargs = {field: None if field not in ("breakdowns",) else ()}
        with pytest.raises(MissingInputError):
            validate_inputs_complete(_full_inputs(**kwargs))

    def test_evaluate_hypothesis_never_calls_the_model_when_input_is_missing(self):
        def _create(**kwargs):
            raise AssertionError("the model must not be called when required input is missing")

        client = LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
        with pytest.raises(MissingInputError):
            evaluate_hypothesis(_full_inputs(hypothesis_id=None), client=client, system_prompt="test")


class TestCrossCheck:
    def test_mismatched_hypothesis_id_raises(self):
        inputs = _full_inputs(hypothesis_id="hyp-1")
        payload = valid_strategy_research_review_input(hypothesis_id="hyp-DIFFERENT")
        client = _client_for(payload)
        with pytest.raises(StrategyResearchCrossCheckError):
            evaluate_hypothesis(inputs, client=client, system_prompt="test")


class TestPythonOverridesFidelityRating:
    def test_matching_rating_passes_through_unchanged(self):
        inputs = _full_inputs(fidelity_practicality=make_fidelity_practicality_context(rating="LOW"))
        payload = valid_strategy_research_review_input(fidelity_practicality_rating="LOW", recommendation="proceed_to_backtest")
        client = _client_for(payload)
        result = evaluate_hypothesis(inputs, client=client, system_prompt="test")
        assert result.review.fidelity_practicality_rating == "LOW"
        assert result.review.recommendation == "proceed_to_backtest"

    def test_disagreeing_rating_is_overwritten_by_python(self):
        inputs = _full_inputs(fidelity_practicality=make_fidelity_practicality_context(rating="MEDIUM"))
        payload = valid_strategy_research_review_input(fidelity_practicality_rating="LOW", recommendation="proceed_to_out_of_sample")
        client = _client_for(payload)
        result = evaluate_hypothesis(inputs, client=client, system_prompt="test")
        assert result.review.fidelity_practicality_rating == "MEDIUM"
        # a MEDIUM override doesn't force a recommendation change
        assert result.review.recommendation == "proceed_to_out_of_sample"

    def test_incompatible_override_also_forces_recommendation_to_escalate(self):
        """If Python's authoritative rating is INCOMPATIBLE but the model
        said LOW + proceed_to_backtest, overriding only the rating field
        would leave an internally inconsistent object (violating the
        same invariant the schema enforces at construction) since
        model_copy does not re-run validators -- this function must fix
        both fields together."""
        inputs = _full_inputs(fidelity_practicality=make_fidelity_practicality_context(rating="INCOMPATIBLE", hard_rejected=True))
        payload = valid_strategy_research_review_input(fidelity_practicality_rating="LOW", recommendation="proceed_to_backtest")
        client = _client_for(payload)
        result = evaluate_hypothesis(inputs, client=client, system_prompt="test")
        assert result.review.fidelity_practicality_rating == "INCOMPATIBLE"
        assert result.review.recommendation == "escalate_for_human_review"

    def test_incompatible_override_leaves_an_already_consistent_reject_alone(self):
        inputs = _full_inputs(fidelity_practicality=make_fidelity_practicality_context(rating="INCOMPATIBLE", hard_rejected=True))
        payload = valid_strategy_research_review_input(fidelity_practicality_rating="LOW", recommendation="reject_hypothesis")
        client = _client_for(payload)
        result = evaluate_hypothesis(inputs, client=client, system_prompt="test")
        assert result.review.fidelity_practicality_rating == "INCOMPATIBLE"
        assert result.review.recommendation == "reject_hypothesis"


class TestHappyPath:
    def test_matching_hypothesis_id_and_rating_round_trips_cleanly(self):
        inputs = _full_inputs()
        payload = valid_strategy_research_review_input()
        client = _client_for(payload)
        result = evaluate_hypothesis(inputs, client=client, system_prompt="test")
        assert result.review.hypothesis_id == "hyp-1"
        assert result.call_result.validated_output is result.review
