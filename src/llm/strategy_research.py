"""Strategy Research orchestration (Step 14): the LLM half of the
Observation -> Hypothesis -> Experimental Strategy Version -> Backtest ->
Validation -> Out-of-Sample Test -> Risk Comparison -> Human Review
pipeline. Every quantitative fact this agent may reference was already
computed by Python (`src.research.performance_breakdown`,
`src.research.overfitting_guards`, `src.research.fidelity_practicality`)
— this module never runs a backtest, never scores overfitting risk, and
never classifies Fidelity practicality itself; it only narrates what
Python already computed and proposes a next step.

**This module cannot promote a strategy to production and cannot modify
a production rule.** There is no import of `src.research.promotion`
anywhere in this file, no import of any config-writing utility, and no
filesystem write of any kind — `tests/unit/llm/test_strategy_research_independence.py`
proves both by source inspection, the same technique
`test_devils_advocate_independence.py` already established for a
different boundary."""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.client import AgentCallResult, LLMClient
from src.llm.context import (
    FidelityPracticalityContext,
    OverfittingGuardContext,
    PerformanceBreakdownContext,
    build_agent_context,
)
from src.llm.router import TaskType
from src.llm.schemas import MarketRegimeAssessment, StrategyResearchReview, ensure_strategy_research_review


class MissingInputError(ValueError):
    """Same "may not invent missing data" guarantee as
    `src.llm.portfolio_manager.MissingInputError` /
    `src.llm.devils_advocate.MissingInputError`."""


class StrategyResearchCrossCheckError(ValueError):
    """Raised when the model's own `StrategyResearchReview.hypothesis_id`
    disagrees with the hypothesis actually under review."""


@dataclass(frozen=True)
class StrategyResearchInputs:
    hypothesis_id: str
    hypothesis_statement: str
    breakdowns: tuple[PerformanceBreakdownContext, ...]
    overfitting: OverfittingGuardContext
    fidelity_practicality: FidelityPracticalityContext
    market_regime: MarketRegimeAssessment | None = None


_REQUIRED_FIELDS = ("hypothesis_id", "hypothesis_statement", "breakdowns", "overfitting", "fidelity_practicality")


def validate_inputs_complete(inputs: StrategyResearchInputs) -> None:
    """The concrete mechanism behind "may not invent missing data" —
    mirrors `src.llm.devils_advocate.validate_inputs_complete`."""
    missing = [name for name in _REQUIRED_FIELDS if not getattr(inputs, name)]
    if missing:
        raise MissingInputError(
            f"cannot evaluate a hypothesis with missing required input(s): {missing}. "
            "The Strategy Research Agent does not invent missing data."
        )


def build_strategy_research_context(inputs: StrategyResearchInputs) -> str:
    """Serializes every input into the same JSON-for-the-prompt shape
    `build_agent_context` already establishes for every other agent."""
    validate_inputs_complete(inputs)
    extra: dict[str, object] = {
        "hypothesis_id": inputs.hypothesis_id,
        "hypothesis_statement": inputs.hypothesis_statement,
        "performance_breakdowns": [vars(b) for b in inputs.breakdowns],
        "overfitting_guards": vars(inputs.overfitting),
        "fidelity_practicality": vars(inputs.fidelity_practicality),
    }
    if inputs.market_regime is not None:
        extra["market_regime"] = {
            "regime": inputs.market_regime.regime,
            "commentary": inputs.market_regime.commentary,
            "notable_events": inputs.market_regime.notable_events,
        }
    return build_agent_context(extra=extra)


@dataclass(frozen=True)
class StrategyResearchEvaluation:
    review: StrategyResearchReview
    call_result: AgentCallResult


def evaluate_hypothesis(
    inputs: StrategyResearchInputs,
    *,
    client: LLMClient,
    system_prompt: str,
) -> StrategyResearchEvaluation:
    """Runs one hypothesis-review cycle. After the model's output
    validates and cross-checks, Python's own Fidelity practicality
    classification has the final word: if the model's
    `fidelity_practicality_rating` disagrees with what
    `src.research.fidelity_practicality.classify_fidelity_practicality`
    actually computed, this function overwrites it — the model's rating
    is advisory phrasing here, never authoritative, the same relationship
    an LLM-requested contract count has to `src.risk.trade_risk.size_trade`."""
    validate_inputs_complete(inputs)
    user_content = build_strategy_research_context(inputs)

    call_result = client.complete_structured(
        agent_role="strategy_research",
        task_type=TaskType.STRATEGY_RESEARCH,
        system_prompt=system_prompt,
        user_content=user_content,
        output_schema=StrategyResearchReview,
    )
    review = ensure_strategy_research_review(call_result.validated_output)

    if review.hypothesis_id != inputs.hypothesis_id:
        raise StrategyResearchCrossCheckError(
            f"StrategyResearchReview.hypothesis_id={review.hypothesis_id!r} does not match the "
            f"hypothesis under review ({inputs.hypothesis_id!r})"
        )

    if review.fidelity_practicality_rating != inputs.fidelity_practicality.rating:
        updates: dict[str, object] = {"fidelity_practicality_rating": inputs.fidelity_practicality.rating}
        # `model_copy` does not re-run validators, so if the authoritative
        # rating just became INCOMPATIBLE, force `recommendation` into
        # agreement too -- otherwise the override could silently produce
        # an object that violates the very invariant this schema enforces
        # at construction time (a model_validator only runs on build).
        if inputs.fidelity_practicality.rating == "INCOMPATIBLE" and review.recommendation not in ("reject_hypothesis", "escalate_for_human_review"):
            updates["recommendation"] = "escalate_for_human_review"
        review = review.model_copy(update=updates)

    return StrategyResearchEvaluation(review=review, call_result=call_result)
