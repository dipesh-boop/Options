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


# Step 11: the platform's fixed 18-category invalidation checklist. Every
# `DevilsAdvocateReview` must assess every one of these for every trade —
# `_validate_all_categories_present` below enforces that as a schema
# invariant, not just a persona instruction the model might skip.
RiskCategory = Literal[
    "directional_risk",
    "volatility_expansion",
    "volatility_collapse",
    "gap_risk",
    "liquidity_deterioration",
    "earnings",
    "economic_events",
    "interest_rates",
    "sector_risk",
    "correlation",
    "portfolio_concentration",
    "assignment_risk",
    "early_exercise",
    "dividend_risk",
    "regime_misclassification",
    "technical_breakdown",
    "unexpected_news",
    "execution_risk",
]

_ALL_RISK_CATEGORIES: frozenset[str] = frozenset(RiskCategory.__args__)  # type: ignore[attr-defined]

ProbabilityCategory = Literal["low", "medium", "high"]
SeverityCategory = Literal["low", "medium", "high", "severe"]
PortfolioImpactCategory = Literal["negligible", "minor", "moderate", "major", "severe"]


class RiskCategoryAssessment(_StrictModel):
    """One line of the mandatory 18-category checklist: does this risk
    apply to this specific trade, and why/why not. `applicable=False` is
    a legitimate, expected answer for most categories on most trades —
    the requirement is that every category was actually considered, not
    that every category must be flagged."""

    category: RiskCategory
    applicable: bool
    note: str = Field(min_length=1, max_length=500)


class FailureScenario(_StrictModel):
    """One concrete way this trade could lose money. `probability_category`
    and `severity` are deliberately categorical, never a fabricated
    numeric probability — this agent has no statistical model backing a
    number like "23% chance," and a plausible-looking fake one would be
    worse than an honest qualitative bucket. Where a real deterministic
    probability exists (Python Quant's `probability_of_profit`), it comes
    from `QuantitativeAnalysisContext` as read-only reference data, not
    from a field on this model."""

    scenario: str = Field(min_length=1, max_length=1000)
    probability_category: ProbabilityCategory
    severity: SeverityCategory
    portfolio_impact_category: PortfolioImpactCategory
    warning_indicators: list[str] = Field(min_length=1, max_length=10)
    possible_mitigation: str = Field(min_length=1, max_length=500)

    @field_validator("warning_indicators")
    @classmethod
    def _indicators_non_blank(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("warning_indicators entries must not be blank")
        return v


class FidelityExecutionRiskAssessment(_StrictModel):
    """The gap between analysis time and a human actually entering the
    order in Fidelity Trader+ (Step 11's MANUAL_EXECUTION concern).
    Each field is this agent's qualitative read of that specific risk;
    `reprice_required` is its opinion — `src.llm.devils_advocate`'s
    orchestration layer independently recomputes staleness from the two
    market snapshots it was given and can override this field to `True`
    the same way Python Risk Engine overrides an LLM's sizing request,
    never the reverse."""

    underlying_movement_risk: str = Field(min_length=1, max_length=500)
    spread_movement_risk: str = Field(min_length=1, max_length=500)
    bid_ask_widening_risk: str = Field(min_length=1, max_length=500)
    iv_change_risk: str = Field(min_length=1, max_length=500)
    delta_change_risk: str = Field(min_length=1, max_length=500)
    regime_change_risk: str = Field(min_length=1, max_length=500)
    news_event_risk: str = Field(min_length=1, max_length=500)
    reprice_required: bool


DevilsAdvocateVerdict = Literal["PASS", "CAUTION", "REJECT", "REPRICE_REQUIRED"]


class DevilsAdvocateReview(_StrictModel):
    """devil_advocate agent output (Step 11): a structured attempt to
    invalidate one `TradeProposal`, never to confirm it. Its job is not
    to be agreeable — `why_not_thesis` must always be populated, even for
    a trade this agent ultimately passes, because "why should we not
    make this trade" is asked of every trade, not just weak ones.

    There is no field anywhere on this schema, or on any of the models
    it's built from, of numeric type — same structural guarantee as
    `PortfolioDecision`, and for the same reason: `probability_category`/
    `severity`/`portfolio_impact_category` are categorical precisely so
    this agent cannot fabricate a numeric probability.

    `verdict` can never be "approve" or "execute" — the Devil's Advocate
    cannot approve execution; PASS is the strongest thing it can say, and
    even PASS is not an approval, only "found no disqualifying issue.\""""

    review_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=64)
    verdict: DevilsAdvocateVerdict
    why_not_thesis: str = Field(min_length=1, max_length=2000)
    risk_assessment: list[RiskCategoryAssessment] = Field(min_length=18, max_length=18)
    failure_scenarios: list[FailureScenario] = Field(min_length=3, max_length=10)
    fidelity_execution_risk: FidelityExecutionRiskAssessment
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _require_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    @field_validator("risk_assessment")
    @classmethod
    def _all_18_categories_present_exactly_once(cls, v: list[RiskCategoryAssessment]) -> list[RiskCategoryAssessment]:
        seen = [item.category for item in v]
        if len(set(seen)) != len(seen):
            raise ValueError("each risk category may appear at most once in risk_assessment")
        if set(seen) != _ALL_RISK_CATEGORIES:
            missing = _ALL_RISK_CATEGORIES - set(seen)
            raise ValueError(f"risk_assessment is missing required categories: {sorted(missing)}")
        return v

    @model_validator(mode="after")
    def _reprice_required_verdict_matches_fidelity_assessment(self) -> "DevilsAdvocateReview":
        if self.fidelity_execution_risk.reprice_required and self.verdict not in ("REPRICE_REQUIRED", "REJECT"):
            raise ValueError(
                "fidelity_execution_risk.reprice_required=True is inconsistent with a PASS/CAUTION verdict — "
                "if execution data is stale, the verdict must be REPRICE_REQUIRED (or REJECT, if the trade "
                "should not proceed at all regardless of repricing)."
            )
        return self


def ensure_devils_advocate_review(obj: object) -> DevilsAdvocateReview:
    """Runtime boundary guard, same exact-type pattern as
    `ensure_trade_proposal`/`ensure_portfolio_decision`."""
    if type(obj) is not DevilsAdvocateReview:
        raise TypeError(
            f"Expected a validated DevilsAdvocateReview instance, got {type(obj).__name__!r}. "
            "Only src.llm.schemas.DevilsAdvocateReview may be treated as a Devil's Advocate verdict."
        )
    return obj


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


# What the Portfolio Manager may conclude about one already-proposed
# trade (Step 10). Deliberately not "approve" — this agent has no
# approval authority at all; "propose_advance" names what it actually
# does, which is recommend the proposal continue toward Python Risk
# Engine, the only component that can ever approve anything.
PortfolioDecisionType = Literal["propose_advance", "reject", "hold_cash"]


class PortfolioDecision(_StrictModel):
    """portfolio_manager agent output (Step 10): the CIO-level verdict on
    one specific `TradeProposal`, reached by explicitly working through
    the platform's 13-question decision process. Every field here is
    qualitative narrative, a category, or a plain boolean/list of
    strings — there is no field of numeric type anywhere in this schema.
    That is deliberate, not an oversight: this agent must never originate
    a price, a Greek, a probability, an account balance, or a risk
    figure, and a schema with no numeric slot to put one in is a
    stronger guarantee of that than a comment asking nicely. Every
    quantitative fact this agent's rationale refers to must already have
    come from Python Quant / Python Risk Engine and be referenced by
    proposal_id / decision_id, not restated as a fresh number here.

    This decision is advisory input to the rest of the pipeline, same as
    `AdversarialReview`/`RiskReviewNote` — it carries no authority to
    override `src.risk.engine.evaluate_trade_proposal`, resize a
    position, or change a risk limit, and there is no field on this
    schema through which it could even try."""

    decision_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=64)
    decision: PortfolioDecisionType
    confidence: Conviction
    market_regime: MarketRegimeLabel
    thesis_summary: str = Field(min_length=1, max_length=2000)
    bear_case: str = Field(min_length=1, max_length=2000)
    portfolio_fit: str = Field(min_length=1, max_length=1000)
    correlation_assessment: str = Field(min_length=1, max_length=1000)
    capital_efficiency: str = Field(min_length=1, max_length=1000)
    alternative_considered: str = Field(min_length=1, max_length=1000)
    cash_preferred: bool
    invalidation_conditions: list[str] = Field(min_length=1, max_length=10)
    required_follow_up: list[str] = Field(default_factory=list, max_length=10)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def _require_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    @field_validator("invalidation_conditions", "required_follow_up")
    @classmethod
    def _entries_non_blank(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("entries must not be blank")
        return v

    @model_validator(mode="after")
    def _cash_preferred_matches_decision(self) -> "PortfolioDecision":
        if (self.decision == "hold_cash") != self.cash_preferred:
            raise ValueError(
                "cash_preferred must be True if and only if decision is 'hold_cash' — a decision "
                "to advance or reject while also claiming cash was preferred is a contradiction "
                "this schema refuses to carry silently."
            )
        return self


def ensure_portfolio_decision(obj: object) -> PortfolioDecision:
    """Runtime boundary guard, same exact-type pattern as
    `ensure_trade_proposal` below. Accepts nothing but a genuine,
    already-validated `PortfolioDecision` instance — no dict, no
    subclass, no other schema in this module."""
    if type(obj) is not PortfolioDecision:
        raise TypeError(
            f"Expected a validated PortfolioDecision instance, got {type(obj).__name__!r}. "
            "Only src.llm.schemas.PortfolioDecision may be treated as a Portfolio Manager decision."
        )
    return obj


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
