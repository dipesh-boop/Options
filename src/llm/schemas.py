"""Pydantic structured-output schemas for the LLM orchestration layer.

Every LLM call in this system must return one of these types — never a
raw string, dict, or free-form JSON that the rest of the platform trusts
as-is (src/llm/client.py enforces this at the API boundary).

`TradeProposal` is the ONLY schema that may ever reach anywhere near an
order. It is declarative intent, not an instruction: no order id, no
broker field, no submit/execute flag, no computed risk number the system
would trust. Every instance must still be repriced by Python Quant and
gated by Python Risk Engine (not implemented yet — see
IMPLEMENTATION_PLAN.md Phase 1) before it can become anything real. No
other schema in this module is ever allowed to reach that path.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class _StrictModel(BaseModel):
    """Base for every LLM-facing schema. `extra="forbid"` means an LLM
    (or a hand-crafted malicious payload) trying to smuggle an
    execution-shaped field — `order_id`, `submit`, `execute`, `broker`,
    `quantity_override`, anything — through an otherwise valid-looking
    payload fails validation outright instead of being silently
    coerced or ignored. `frozen=True` means nothing downstream can mutate
    a validated instance into something it wasn't."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyType(str, Enum):
    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"
    PUT_CREDIT_SPREAD = "put_credit_spread"


class TradeAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    ROLL = "roll"


class Conviction(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskFlag(_StrictModel):
    code: str = Field(min_length=1, max_length=64)
    severity: Literal["info", "caution", "warning"]
    note: str = Field(min_length=1, max_length=500)


class StructureIntent(_StrictModel):
    """A declarative trade shape. Every field here is a *stated intent*,
    not a computed, trusted number. Python Quant reprices whatever
    contract this resolves to from the live market snapshot; nothing
    here is read as an authoritative Greek, price, or max-loss figure —
    `approx_target_delta` in particular is directional guidance only."""

    symbol: str = Field(min_length=1, max_length=10)
    strategy_type: StrategyType
    action: TradeAction
    target_dte_min: int = Field(ge=0, le=365)
    target_dte_max: int = Field(ge=0, le=365)
    approx_target_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("target_dte_max")
    @classmethod
    def _dte_max_at_least_min(cls, v: int, info: ValidationInfo) -> int:
        dte_min = info.data.get("target_dte_min")
        if dte_min is not None and v < dte_min:
            raise ValueError("target_dte_max must be >= target_dte_min")
        return v


class TradeProposal(_StrictModel):
    """The ONLY schema an LLM may emit that is allowed anywhere near an
    order. See module docstring — this is intent, never an instruction."""

    proposal_id: str = Field(min_length=1, max_length=64)
    source_agent: str = Field(min_length=1, max_length=64)
    structure: StructureIntent
    rationale: str = Field(min_length=1, max_length=2000)
    conviction: Conviction
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    rank: int = Field(ge=1)


class MarketRegimeAssessment(_StrictModel):
    """market_regime agent output. Qualitative only — classifies a
    regime the caller already measured; never invents an index level."""

    regime: Literal["low_vol", "normal", "elevated_vol", "crisis"]
    commentary: str = Field(min_length=1, max_length=2000)
    notable_events: list[str] = Field(default_factory=list)


class CandidateHighlight(_StrictModel):
    symbol: str = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=500)
    conviction: Conviction


class OpportunityScan(_StrictModel):
    """opportunity_scanner agent output. Prioritizes/annotates candidates
    the deterministic Strategy Screener already approved — never adds a
    symbol the screener didn't surface."""

    highlights: list[CandidateHighlight]
    summary: str = Field(min_length=1, max_length=1000)


class StrategyAnalystOutput(_StrictModel):
    """strategy_analyst agent output: proposed structures for the
    Portfolio Manager and Devil's Advocate to work with."""

    proposals: list[TradeProposal]


class AdversarialReview(_StrictModel):
    """devil_advocate agent output. `do_not_advance` is an advisory
    quality flag only — it has no authority over Python Risk Engine and
    changes nothing about what that engine allows."""

    proposal_id: str = Field(min_length=1, max_length=64)
    critique: str = Field(min_length=1, max_length=2000)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    do_not_advance: bool = False


class RiskReviewNote(_StrictModel):
    """risk_reviewer agent output: an independent qualitative second
    opinion, distinct from and subordinate to Python Risk Engine, which
    remains the sole authority on what is actually allowed."""

    proposal_id: str = Field(min_length=1, max_length=64)
    concerns: list[str] = Field(default_factory=list)
    concurs_with_quant_review: bool
    note: str = Field(min_length=1, max_length=1000)


class TradeManagerOutput(_StrictModel):
    """trade_manager agent output: a narrative plus any suggested
    close/roll actions on existing positions, expressed only as
    TradeProposal objects (action=close|roll) like everything else."""

    summary: str = Field(min_length=1, max_length=2000)
    proposals: list[TradeProposal] = Field(default_factory=list)


class PerformanceAuditReport(_StrictModel):
    """performance_auditor agent output: journal_analysis (routine) or
    weekly_portfolio_review (high-reasoning) — never a trade proposal."""

    period_start: date
    period_end: date
    summary: str = Field(min_length=1, max_length=3000)
    lessons_learned: list[str] = Field(default_factory=list)


class PortfolioManagerReview(_StrictModel):
    """portfolio_manager agent output: the top-of-pipeline synthesis. A
    ranked shortlist of TradeProposals plus the rationale for the
    ranking — nothing else leaves this schema that the rest of the
    system could mistake for a computed number or an execution signal."""

    proposals: list[TradeProposal]
    summary: str = Field(min_length=1, max_length=2000)


def ensure_trade_proposal(obj: object) -> TradeProposal:
    """Runtime boundary guard. Every point where agent output could flow
    toward Python Quant / Python Risk Engine (not implemented yet) must
    call this first. It accepts nothing but a genuine, already-validated
    `TradeProposal` instance:

    - a plain dict, even one with exactly the right keys and values, is
      rejected — structural resemblance is not validation;
    - an instance of any other schema in this module is rejected;
    - an instance of a subclass of `TradeProposal` is rejected too (exact
      type check, not `isinstance`), so a hand-rolled subclass can't
      smuggle extra attributes past callers that only check `isinstance`.
    """
    if type(obj) is not TradeProposal:
        raise TypeError(
            f"Expected a validated TradeProposal instance, got {type(obj).__name__!r}. "
            "Only src.llm.schemas.TradeProposal may reach the execution boundary."
        )
    return obj
