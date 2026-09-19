"""The single choke point a hypothesis must pass through to become a
production strategy version — the "PROMOTION" section of Step 14.

**The Strategy Research Agent cannot promote a strategy.** Nothing in
`src.llm.strategy_research` constructs a `PromotionRequest` or calls
`promote_strategy`; `StrategyResearchReview` (the LLM's entire output
schema) has no `human_approved` field or anything resembling one — there
is no value the model could return that would satisfy this function's
gate. Promotion requires successful testing (validation *and*
out-of-sample, both Python-computed), a Risk Engine review verdict of
PASS (`src.research.risk_review`), and explicit human approval — all
four independently required, all four checked here, none of them
satisfiable by an LLM call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.research.risk_review import StrategyRiskReview


class PromotionError(ValueError):
    """Raised when a promotion attempt is missing any one of the four
    required conditions. Never caught and silently retried with a
    relaxed check — a rejected promotion is rejected."""


@dataclass(frozen=True)
class PromotionRequest:
    hypothesis_id: str
    strategy_version: str
    validation_passed: bool
    out_of_sample_passed: bool
    risk_review: StrategyRiskReview
    human_approved: bool
    human_approver: str | None = None
    approval_notes: str | None = None


@dataclass(frozen=True)
class PromotionRecord:
    hypothesis_id: str
    strategy_version: str
    promoted_at: datetime
    human_approver: str
    approval_notes: str | None


def promote_strategy(request: PromotionRequest) -> PromotionRecord:
    """All four gates are independently required — this is an AND, never
    an OR, and none of them may be inferred from the others (a PASS risk
    review does not imply out-of-sample success; human approval does not
    imply a risk review even happened)."""
    reasons: list[str] = []
    if not request.validation_passed:
        reasons.append("validation not passed")
    if not request.out_of_sample_passed:
        reasons.append("out-of-sample test not passed")
    if request.risk_review.verdict != "PASS":
        reasons.append(f"risk review verdict is {request.risk_review.verdict!r}, not PASS")
    if not request.human_approved or not request.human_approver:
        reasons.append("human approval missing")

    if reasons:
        raise PromotionError(
            f"cannot promote strategy_version={request.strategy_version!r} "
            f"(hypothesis_id={request.hypothesis_id!r}): " + "; ".join(reasons)
        )

    return PromotionRecord(
        hypothesis_id=request.hypothesis_id,
        strategy_version=request.strategy_version,
        promoted_at=datetime.now(timezone.utc),
        human_approver=request.human_approver,  # type: ignore[arg-type]
        approval_notes=request.approval_notes,
    )
