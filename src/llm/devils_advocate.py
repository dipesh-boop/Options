"""Devil's Advocate orchestration (Step 11): the independent adversarial
review layer whose job is to attempt to invalidate a proposed trade —
never to confirm it.

**Independence** (Step 11's explicit requirement): this module never
accepts, builds a prompt from, or passes along a `PortfolioDecision` (or
anything derived from one) anywhere in its inputs or call graph. There is
no import of `src.llm.portfolio_manager`/`PortfolioDecision` in this
file at all, and no parameter on `DevilsAdvocateInputs` or
`evaluate_trade_risk` through which the Portfolio Manager's eventual
decision could reach this agent before it completes its own analysis —
"avoid having the system grade its own conclusion" is enforced by there
being no conclusion to grade in scope, structurally, not by convention.
See `tests/unit/llm/test_devils_advocate_independence.py`.

Every quantitative fact this agent may reference was already computed by
Python (`QuantitativeAnalysisContext`) or is Python arithmetic performed
in this module (`compute_staleness`) — never a number the model invents.
`DevilsAdvocateReview`'s own schema additionally makes fabricating a
numeric probability structurally impossible (categorical fields only).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.client import AgentCallResult, LLMClient
from src.llm.context import (
    MarketSnapshotContext,
    PortfolioStateContext,
    QuantitativeAnalysisContext,
    WheelReviewContext,
    build_agent_context,
)
from src.llm.router import TaskType
from src.llm.schemas import (
    DevilsAdvocateReview,
    MarketRegimeAssessment,
    TradeProposal,
    ensure_devils_advocate_review,
    ensure_trade_proposal,
)

# Thresholds beyond which Python independently decides execution data has
# gone stale between analysis and a human entering the order in Fidelity
# Trader+, regardless of what the model's own
# `FidelityExecutionRiskAssessment.reprice_required` says. These are
# advisory-agent thresholds, not a Risk Engine limit — nothing here gates
# an actual trade the way `src.risk.limits` does — so they're plain
# module constants rather than a YAML config.
MAX_UNDERLYING_MOVE_PCT = 0.005  # 0.5% underlying move
MAX_SPREAD_WIDENING_PCT = 0.50  # 50% relative widening of the bid/ask spread
MAX_IV_CHANGE_ABS = 0.03  # 3 vol points
MAX_DELTA_CHANGE_ABS = 0.05
MAX_SNAPSHOT_AGE_SECONDS = 900.0  # 15 minutes, matching the platform's other freshness windows


class MissingInputError(ValueError):
    """Same "may not invent missing data" guarantee as
    `src.llm.portfolio_manager.MissingInputError`."""


class DevilsAdvocateCrossCheckError(ValueError):
    """Raised when the model's own `DevilsAdvocateReview.proposal_id`
    disagrees with the proposal actually under review."""


@dataclass(frozen=True)
class DevilsAdvocateInputs:
    """Every input this agent needs to attempt to invalidate one trade.
    Deliberately absent: anything from the Portfolio Manager — see this
    module's docstring. `earnings_in_window` is a plain Python-computed
    boolean (never "unknown"; the caller resolves it, e.g. via
    `src.data.earnings.is_within_earnings_window`, before constructing
    this object), not a field this agent could leave ambiguous."""

    proposal: TradeProposal
    quant_analysis: QuantitativeAnalysisContext
    portfolio_state: PortfolioStateContext
    market_regime: MarketRegimeAssessment
    analysis_snapshot: MarketSnapshotContext
    current_snapshot: MarketSnapshotContext
    earnings_in_window: bool = False
    # Step 22.2, Part 17: present only when the proposal under review is
    # a leg of a Wheel (src.wheel.review_context.build_wheel_review_context) --
    # absent for every other proposal, never fabricated.
    wheel_context: WheelReviewContext | None = None


_REQUIRED_FIELDS = ("proposal", "quant_analysis", "portfolio_state", "market_regime", "analysis_snapshot", "current_snapshot")


def validate_inputs_complete(inputs: DevilsAdvocateInputs) -> None:
    """The concrete mechanism behind "may not invent missing data" —
    mirrors `src.llm.portfolio_manager.validate_inputs_complete`."""
    missing = [name for name in _REQUIRED_FIELDS if getattr(inputs, name) is None]
    if missing:
        raise MissingInputError(
            f"cannot evaluate a proposal with missing required input(s): {missing}. "
            "The Devil's Advocate does not invent missing data."
        )
    ensure_trade_proposal(inputs.proposal)


@dataclass(frozen=True)
class StalenessCheck:
    """Python's own deterministic answer to "has this gone stale between
    analysis and a human entering it," computed from two market
    snapshots — never estimated by the model."""

    underlying_move_pct: float
    spread_widening_pct: float
    iv_change_abs: float | None
    delta_change_abs: float | None
    snapshot_age_seconds: float
    stale: bool
    reasons: list[str]


def compute_staleness(analysis: MarketSnapshotContext, current: MarketSnapshotContext) -> StalenessCheck:
    if current.as_of < analysis.as_of:
        raise ValueError("current_snapshot cannot be earlier than analysis_snapshot")
    if analysis.underlying_price <= 0:
        raise ValueError("analysis_snapshot.underlying_price must be positive")

    age = (current.as_of - analysis.as_of).total_seconds()
    underlying_move_pct = abs(current.underlying_price - analysis.underlying_price) / analysis.underlying_price

    analysis_spread = analysis.ask - analysis.bid
    current_spread = current.ask - current.bid
    if analysis_spread > 0:
        spread_widening_pct = (current_spread - analysis_spread) / analysis_spread
    else:
        spread_widening_pct = 0.0 if current_spread <= 0 else float("inf")

    iv_change = abs(current.iv - analysis.iv) if (current.iv is not None and analysis.iv is not None) else None
    delta_change = (
        abs(current.delta - analysis.delta) if (current.delta is not None and analysis.delta is not None) else None
    )

    reasons: list[str] = []
    if underlying_move_pct > MAX_UNDERLYING_MOVE_PCT:
        reasons.append(f"underlying moved {underlying_move_pct:.2%}, exceeding {MAX_UNDERLYING_MOVE_PCT:.2%}")
    if spread_widening_pct > MAX_SPREAD_WIDENING_PCT:
        reasons.append(f"bid/ask spread widened {spread_widening_pct:.0%}, exceeding {MAX_SPREAD_WIDENING_PCT:.0%}")
    if iv_change is not None and iv_change > MAX_IV_CHANGE_ABS:
        reasons.append(f"IV moved {iv_change:.2f}, exceeding {MAX_IV_CHANGE_ABS:.2f}")
    if delta_change is not None and delta_change > MAX_DELTA_CHANGE_ABS:
        reasons.append(f"delta moved {delta_change:.2f}, exceeding {MAX_DELTA_CHANGE_ABS:.2f}")
    if age > MAX_SNAPSHOT_AGE_SECONDS:
        reasons.append(f"snapshot age {age:.0f}s exceeds {MAX_SNAPSHOT_AGE_SECONDS:.0f}s")

    return StalenessCheck(
        underlying_move_pct=underlying_move_pct,
        spread_widening_pct=spread_widening_pct,
        iv_change_abs=iv_change,
        delta_change_abs=delta_change,
        snapshot_age_seconds=age,
        stale=bool(reasons),
        reasons=reasons,
    )


def build_devils_advocate_context(inputs: DevilsAdvocateInputs) -> str:
    """Serializes every input, plus the deterministic staleness
    computation, into the same JSON-for-the-prompt shape
    `src.llm.context.build_agent_context` already establishes."""
    validate_inputs_complete(inputs)
    staleness = compute_staleness(inputs.analysis_snapshot, inputs.current_snapshot)
    extra: dict[str, object] = {
        "proposal": inputs.proposal.model_dump(mode="json"),
        "quantitative_analysis": vars(inputs.quant_analysis),
        "market_regime": {
            "regime": inputs.market_regime.regime,
            "commentary": inputs.market_regime.commentary,
            "notable_events": inputs.market_regime.notable_events,
        },
        "analysis_snapshot": vars(inputs.analysis_snapshot),
        "current_snapshot": vars(inputs.current_snapshot),
        "computed_staleness": vars(staleness),
        "earnings_in_window": inputs.earnings_in_window,
        "wheel_context": vars(inputs.wheel_context) if inputs.wheel_context is not None else None,
    }
    return build_agent_context(portfolio=inputs.portfolio_state, extra=extra)


@dataclass(frozen=True)
class DevilsAdvocateEvaluation:
    """What one evaluation cycle produces: the validated (and possibly
    Python-overridden — see `evaluate_trade_risk`) review, the raw call
    metadata, and the deterministic staleness figures it was overridden
    with or confirmed against."""

    review: DevilsAdvocateReview
    call_result: AgentCallResult
    staleness: StalenessCheck


def evaluate_trade_risk(
    inputs: DevilsAdvocateInputs,
    *,
    client: LLMClient,
    system_prompt: str,
) -> DevilsAdvocateEvaluation:
    """Runs one full Devil's Advocate evaluation cycle. After the model's
    output validates and cross-checks, Python's own `compute_staleness`
    has the final word on execution staleness: if it says stale and the
    model didn't already conclude REPRICE_REQUIRED or REJECT, this
    function overrides the verdict itself — the model's own
    `FidelityExecutionRiskAssessment` is advisory input here, never
    authoritative, the same relationship an LLM-requested contract count
    has to `src.risk.trade_risk.size_trade`."""
    validate_inputs_complete(inputs)
    staleness = compute_staleness(inputs.analysis_snapshot, inputs.current_snapshot)
    user_content = build_devils_advocate_context(inputs)

    call_result = client.complete_structured(
        agent_role="devil_advocate",
        task_type=TaskType.ADVERSARIAL_TRADE_REVIEW,
        system_prompt=system_prompt,
        user_content=user_content,
        output_schema=DevilsAdvocateReview,
    )
    review = ensure_devils_advocate_review(call_result.validated_output)

    if review.proposal_id != inputs.proposal.proposal_id:
        raise DevilsAdvocateCrossCheckError(
            f"DevilsAdvocateReview.proposal_id={review.proposal_id!r} does not match the proposal "
            f"under review ({inputs.proposal.proposal_id!r})"
        )

    if staleness.stale and review.verdict not in ("REPRICE_REQUIRED", "REJECT"):
        review = review.model_copy(
            update={
                "verdict": "REPRICE_REQUIRED",
                "fidelity_execution_risk": review.fidelity_execution_risk.model_copy(
                    update={"reprice_required": True}
                ),
            }
        )

    return DevilsAdvocateEvaluation(review=review, call_result=call_result, staleness=staleness)
