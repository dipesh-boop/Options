"""Portfolio Manager orchestration (Step 10): the CIO-level review layer
that evaluates one already-proposed trade against the platform's
13-question decision process and produces a `PortfolioDecision`.

This module never computes a price, a Greek, a probability, or a risk
figure — every quantitative input it hands the model was already
computed by Python (`QuantitativeAnalysisContext`, `RiskEngineContext`,
both plain read-only dataclasses in `src.llm.context`, never imported
from `src.quant`/`src.risk` directly — see this module's docstring
pattern in `context.py`). It has no authority to override
`src.risk.engine.evaluate_trade_proposal`'s decision, resize a position,
or change a risk limit: `PortfolioDecision` has no field through which
any of those would even be expressible, and this module never treats
`decision.decision == "propose_advance"` as anything more than a
recommendation logged alongside, never in place of, the Risk Engine's
own result.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.client import AgentCallResult, LLMClient
from src.llm.context import (
    PortfolioStateContext,
    QuantitativeAnalysisContext,
    RiskEngineContext,
    WheelReviewContext,
    build_agent_context,
)
from src.llm.router import TaskType
from src.llm.schemas import (
    AdversarialReview,
    CandidateHighlight,
    MarketRegimeAssessment,
    PortfolioDecision,
    RiskReviewNote,
    TradeProposal,
    ensure_portfolio_decision,
    ensure_trade_proposal,
)


class MissingInputError(ValueError):
    """Raised when one of the Portfolio Manager's required inputs is
    absent. This agent may not invent a missing input — a gap here must
    stop the evaluation, never be silently filled with a guess or a
    default."""


class PortfolioManagerCrossCheckError(ValueError):
    """Raised when the model's own `PortfolioDecision.proposal_id` (or
    `market_regime`) disagrees with the proposal actually under review —
    proof the model didn't just echo the input it was given, or drifted
    onto a different proposal mid-response."""


@dataclass(frozen=True)
class PortfolioManagerInputs:
    """Every input Step 10 names, bundled for one evaluation cycle.

    `risk_engine_result` is **optional as of Step 12**: Step 12's
    required order-pipeline flow (TradeProposal -> Quant -> Devil's
    Advocate -> Portfolio Manager -> Risk Engine -> ...) places the
    Portfolio Manager *before* Python Risk Engine ever runs, so it
    cannot be a hard requirement here without making that pipeline
    ordering impossible. When it is supplied (e.g. a later re-review
    cycle that already has a Risk Engine result to narrate around),
    the Portfolio Manager still treats it exactly as before — read-only
    reference data it narrates around, never overrides. The other six
    fields remain required; `opportunity_highlight` is, as originally,
    the one field that was always legitimately optional."""

    proposal: TradeProposal
    market_regime: MarketRegimeAssessment
    quant_analysis: QuantitativeAnalysisContext
    devil_advocate_review: AdversarialReview
    risk_reviewer_note: RiskReviewNote
    portfolio_state: PortfolioStateContext
    risk_engine_result: RiskEngineContext | None = None
    opportunity_highlight: CandidateHighlight | None = None
    # Step 22.2, Part 18: present only when the proposal under review is
    # a leg of a Wheel (src.wheel.review_context.build_wheel_review_context).
    wheel_context: WheelReviewContext | None = None


_REQUIRED_FIELDS = (
    "proposal",
    "market_regime",
    "quant_analysis",
    "devil_advocate_review",
    "risk_reviewer_note",
    "portfolio_state",
)


def validate_inputs_complete(inputs: PortfolioManagerInputs) -> None:
    """The concrete mechanism behind "may not invent missing inputs":
    every required field must actually be present before this module
    will build a prompt or call the model at all."""
    missing = [name for name in _REQUIRED_FIELDS if getattr(inputs, name) is None]
    if missing:
        raise MissingInputError(
            f"cannot evaluate a proposal with missing required input(s): {missing}. "
            "The Portfolio Manager does not invent missing data."
        )
    # Same exact-type boundary guard used everywhere else a TradeProposal
    # is about to be acted on — a dict or forged object with matching
    # attributes is rejected here too, not just downstream.
    ensure_trade_proposal(inputs.proposal)


def build_portfolio_manager_context(inputs: PortfolioManagerInputs) -> str:
    """Serializes every input into the same JSON-for-the-prompt shape
    `src.llm.context.build_agent_context` already establishes — reused,
    not reimplemented, via its `extra` escape hatch for the fields that
    builder doesn't already have a named parameter for."""
    validate_inputs_complete(inputs)
    extra: dict[str, object] = {
        "proposal": inputs.proposal.model_dump(mode="json"),
        "market_regime": {
            "regime": inputs.market_regime.regime,
            "commentary": inputs.market_regime.commentary,
            "notable_events": inputs.market_regime.notable_events,
        },
        "quantitative_analysis": vars(inputs.quant_analysis),
        "devil_advocate_review": {
            "critique": inputs.devil_advocate_review.critique,
            "risk_flags": [f.model_dump() for f in inputs.devil_advocate_review.risk_flags],
            "do_not_advance": inputs.devil_advocate_review.do_not_advance,
        },
        "risk_reviewer_note": {
            "concerns": inputs.risk_reviewer_note.concerns,
            "concurs_with_quant_review": inputs.risk_reviewer_note.concurs_with_quant_review,
            "note": inputs.risk_reviewer_note.note,
        },
        "risk_engine_result": vars(inputs.risk_engine_result) if inputs.risk_engine_result is not None else None,
        "opportunity_highlight": vars(inputs.opportunity_highlight) if inputs.opportunity_highlight else None,
        "wheel_context": vars(inputs.wheel_context) if inputs.wheel_context is not None else None,
    }
    return build_agent_context(portfolio=inputs.portfolio_state, extra=extra)


@dataclass(frozen=True)
class PortfolioManagerEvaluation:
    """What one evaluation cycle produces: the validated decision plus
    the raw call metadata (`src.llm.audit.record_decision` turns this
    into a persisted, append-only audit entry)."""

    decision: PortfolioDecision
    call_result: AgentCallResult


def evaluate_proposal(
    inputs: PortfolioManagerInputs,
    *,
    client: LLMClient,
    system_prompt: str,
) -> PortfolioManagerEvaluation:
    """Runs one full Portfolio Manager evaluation cycle: build context
    from Python-verified inputs only, call the model for a structured
    `PortfolioDecision`, validate it through the same boundary guard
    every other schema in this codebase uses, and cross-check it against
    the proposal actually under review."""
    user_content = build_portfolio_manager_context(inputs)

    call_result = client.complete_structured(
        agent_role="portfolio_manager",
        task_type=TaskType.PORTFOLIO_MANAGER,
        system_prompt=system_prompt,
        user_content=user_content,
        output_schema=PortfolioDecision,
    )
    decision = ensure_portfolio_decision(call_result.validated_output)

    if decision.proposal_id != inputs.proposal.proposal_id:
        raise PortfolioManagerCrossCheckError(
            f"PortfolioDecision.proposal_id={decision.proposal_id!r} does not match the proposal "
            f"under review ({inputs.proposal.proposal_id!r})"
        )
    if decision.market_regime != inputs.market_regime.regime:
        raise PortfolioManagerCrossCheckError(
            f"PortfolioDecision.market_regime={decision.market_regime!r} does not match the "
            f"supplied market_regime ({inputs.market_regime.regime!r})"
        )

    return PortfolioManagerEvaluation(decision=decision, call_result=call_result)
