"""Pydantic structured-output schemas for the LLM orchestration layer.

Every LLM call in this system must return one of these types — never a
raw string, dict, or free-form JSON that the rest of the platform trusts
as-is (src/llm/client.py enforces this at the API boundary).

`TradeProposal` is the ONLY schema that may ever reach anywhere near an
order. It is declarative intent, not an instruction: no final approved
contract count, no authoritative max loss, no portfolio risk figure, no
broker order id, no execution authorization — those all belong to
deterministic downstream systems (Python Quant / Python Risk Engine, not
implemented yet — see IMPLEMENTATION_PLAN.md Phase 1) that reprice and
gate every proposal before it can become anything real. No other schema
in this module is ever allowed to reach that path.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

# Maximum age of the underlying market data snapshot a TradeProposal may be
# built from, measured as (proposal timestamp - data_timestamp). A proposal
# whose market data is older than this is rejected as stale.
#
# TODO(Phase 0): this is a placeholder default, not a researched policy
# value — move it to config once config/risk.yaml (or similar) exists,
# the same way model routing moved out of code into config/llm.yaml.
MAX_MARKET_DATA_AGE = timedelta(minutes=15)


class _StrictModel(BaseModel):
    """Base for every LLM-facing schema. `extra="forbid"` means an LLM
    (or a hand-crafted malicious payload) trying to smuggle an
    execution-shaped field — `order_id`, `submit`, `execute`, `broker`,
    `final_approved_contracts`, `authoritative_max_loss`, `portfolio_risk`,
    `execution_authorization`, anything — through an otherwise
    valid-looking payload fails validation outright instead of being
    silently coerced or ignored. `frozen=True` means nothing downstream
    can mutate a validated instance into something it wasn't."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyType(str, Enum):
    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"
    PUT_CREDIT_SPREAD = "put_credit_spread"


class TradeAction(str, Enum):
    """What this proposal asks to do to the position as a whole. Not part
    of the platform's originally specified TradeProposal field list, kept
    as an additive field (default OPEN) so the Trade Manager role can
    still propose closing/rolling an existing position through the same
    schema — see .claude/agents/trade_manager.md."""

    OPEN = "open"
    CLOSE = "close"
    ROLL = "roll"


class Conviction(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TradeDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class LegSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


# Shared with MarketRegimeAssessment.regime below so a TradeProposal's
# stated market_regime always uses the same vocabulary the market_regime
# agent produces.
MarketRegimeLabel = Literal["low_vol", "normal", "elevated_vol", "crisis"]


class RiskFlag(_StrictModel):
    code: str = Field(min_length=1, max_length=64)
    severity: Literal["info", "caution", "warning"]
    note: str = Field(min_length=1, max_length=500)


class OptionLeg(_StrictModel):
    """One leg of a proposed structure. All legs of a TradeProposal share
    its top-level `expiration` — every initial strategy (CSP, covered
    call, put credit spread) is single-expiration, so a per-leg expiry
    field would only add a redundant place for the two to disagree."""

    right: OptionRight
    strike: float = Field(gt=0)
    side: LegSide


class TradeProposal(_StrictModel):
    """The ONLY schema an LLM may emit that is allowed anywhere near an
    order. See module docstring — this is intent, never an instruction.

    Deliberately absent, and rejected outright if smuggled in via extra
    fields (`extra="forbid"` on `_StrictModel`): a final approved contract
    count, an authoritative maximum loss, a portfolio risk figure, a
    broker order id, or any execution authorization. Those are computed
    and enforced only by Python Quant / Python Risk Engine.
    """

    proposal_id: str = Field(min_length=1, max_length=64)
    timestamp: datetime
    ticker: str = Field(pattern=r"^[A-Z]{1,10}$")
    strategy: StrategyType
    market_regime: MarketRegimeLabel
    expiration: date
    legs: list[OptionLeg] = Field(min_length=1, max_length=2)
    direction: TradeDirection
    contracts_requested: int = Field(ge=1, le=100_000)
    target_entry: float = Field(gt=0)
    profit_target: float = Field(gt=0, le=1)
    management_dte: int = Field(ge=0, le=365)
    thesis: str = Field(min_length=1, max_length=2000)
    risk_thesis: str = Field(min_length=1, max_length=2000)
    confidence: Conviction
    data_sources: list[str] = Field(min_length=1, max_length=10)
    data_timestamp: datetime
    invalidation_conditions: list[str] = Field(min_length=1, max_length=10)
    action: TradeAction = TradeAction.OPEN

    @field_validator("timestamp", "data_timestamp")
    @classmethod
    def _require_timezone_aware(cls, v: datetime, info: ValidationInfo) -> datetime:
        if v.tzinfo is None:
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return v

    @field_validator("data_sources")
    @classmethod
    def _sources_non_blank(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("data_sources entries must not be blank")
        return v

    @field_validator("invalidation_conditions")
    @classmethod
    def _invalidation_conditions_non_blank(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("invalidation_conditions entries must not be blank")
        return v

    @model_validator(mode="after")
    def _validate_market_data_freshness(self) -> "TradeProposal":
        """Reject proposals built on stale market data. `data_timestamp`
        from the future (after the proposal itself) is rejected as an
        integrity violation; data older than MAX_MARKET_DATA_AGE relative
        to the proposal's own timestamp is rejected as stale. Anchoring
        on the proposal's own timestamp, rather than wall-clock "now" at
        validation time, keeps this deterministic — a proposal doesn't
        retroactively become "stale" just because it's read back later."""
        if self.data_timestamp > self.timestamp:
            raise ValueError(
                "data_timestamp cannot be after the proposal timestamp "
                f"(data_timestamp={self.data_timestamp!r}, timestamp={self.timestamp!r})"
            )
        age = self.timestamp - self.data_timestamp
        if age > MAX_MARKET_DATA_AGE:
            raise ValueError(
                f"market data is stale: {age} old, exceeds max allowed age of {MAX_MARKET_DATA_AGE}"
            )
        return self

    @model_validator(mode="after")
    def _validate_expiration_after_timestamp(self) -> "TradeProposal":
        if self.expiration <= self.timestamp.date():
            raise ValueError("expiration must be after the proposal timestamp's date")
        return self

    @model_validator(mode="after")
    def _validate_management_dte_within_expiration_window(self) -> "TradeProposal":
        total_dte = (self.expiration - self.timestamp.date()).days
        if self.management_dte > total_dte:
            raise ValueError(
                f"management_dte ({self.management_dte}) cannot exceed the "
                f"structure's total DTE ({total_dte})"
            )
        return self

    @model_validator(mode="after")
    def _validate_legs_match_strategy(self) -> "TradeProposal":
        if self.strategy == StrategyType.CASH_SECURED_PUT:
            if len(self.legs) != 1 or self.legs[0].right != OptionRight.PUT or self.legs[0].side != LegSide.SELL:
                raise ValueError("cash_secured_put requires exactly one short put leg")

        elif self.strategy == StrategyType.COVERED_CALL:
            if len(self.legs) != 1 or self.legs[0].right != OptionRight.CALL or self.legs[0].side != LegSide.SELL:
                raise ValueError("covered_call requires exactly one short call leg")

        elif self.strategy == StrategyType.PUT_CREDIT_SPREAD:
            if len(self.legs) != 2:
                raise ValueError("put_credit_spread requires exactly two legs")
            if any(leg.right != OptionRight.PUT for leg in self.legs):
                raise ValueError("put_credit_spread legs must both be puts")
            sells = [leg for leg in self.legs if leg.side == LegSide.SELL]
            buys = [leg for leg in self.legs if leg.side == LegSide.BUY]
            if len(sells) != 1 or len(buys) != 1:
                raise ValueError("put_credit_spread requires exactly one short leg and one long leg")
            if sells[0].strike <= buys[0].strike:
                raise ValueError(
                    "put_credit_spread short put strike must be higher than the long put "
                    "strike (net credit structure)"
                )
        return self


class MarketRegimeAssessment(_StrictModel):
    """market_regime agent output. Qualitative only — classifies a
    regime the caller already measured; never invents an index level."""

    regime: MarketRegimeLabel
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
    shortlist of TradeProposals plus the rationale behind it — nothing
    else leaves this schema that the rest of the system could mistake for
    a computed number or an execution signal."""

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
