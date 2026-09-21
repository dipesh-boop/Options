"""Step 22: the two record shapes `SqliteValidationStore` gained beyond
Step 19's original trades/snapshots/rejected_outcomes/violations --
`CohortRecord` (the cohort's own identity/status bookkeeping, wrapping
the `StrategyVersionManifest` `src.validation.protocol` already builds)
and `OpportunityRecord` (the full per-opportunity decision trail: every
strategy alternative considered, at decision time, plus the actual
pipeline outcome for whichever one was proposed).

`OpportunityRecord` deliberately does not redefine any of these shapes
-- it carries the SAME `QuantitativeAnalysis`/`DevilsAdvocateReview`/
`PortfolioDecision`/`RiskDecisionResult`/`Order`/`FidelityTradeTicket`/
`StrategyAlternativeRecord` objects `src.orchestration.pipeline`/
`src.validation.counterfactual` already produce, so persisting an
opportunity can never drift from what those modules actually computed.

Serialization uses `pydantic.TypeAdapter`, which pydantic v2 supports
natively for both `BaseModel` subclasses and plain `@dataclass`
classes (enums, nested dataclasses, tuples, and `math.inf` all
round-trip exactly through `dump_python(mode="json")` /
`validate_python`) -- this is a generic mechanism, not a hand-written
per-field converter that could silently drop a field a future schema
change adds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import TypeAdapter

from src.llm.schemas import DevilsAdvocateReview, PortfolioDecision
from src.risk.engine import RiskDecisionResult
from src.risk.trade_risk import QuantitativeAnalysis
from src.validation.counterfactual import StrategyAlternativeRecord
from src.validation.protocol import StrategyVersionManifest

T = TypeVar("T")


def to_jsonable(value: Any, type_: type) -> Any:
    """Serialize any dataclass or pydantic model (or a `list`/`tuple`/
    `None` of one) to a plain JSON-safe Python structure -- a generic
    replacement for a hand-written `_x_to_dict` per record type."""
    return TypeAdapter(type_).dump_python(value, mode="json")


def from_jsonable(data: Any, type_: type[T]) -> T:
    """The exact inverse of `to_jsonable` -- reconstructs the original
    typed object (including enums, nested dataclasses/models, and
    `math.inf`) from the plain structure `to_jsonable`/`json.loads`
    produced."""
    return TypeAdapter(type_).validate_python(data)


CohortStatus = Literal["created", "active", "completed", "aborted"]


@dataclass(frozen=True)
class CohortRecord:
    """One validation cohort's identity and lifecycle status. Wraps
    (never duplicates) `StrategyVersionManifest` -- every field Part 3
    of the Step 22 instruction names (cohort_id, validation_version,
    strategy/risk/quant/prompt/model/execution-model version, manifest
    hash) already lives on the manifest itself
    (`manifest_id`/`strategy_versions`/`config_file_hashes`); this
    record adds only the lifecycle bookkeeping a frozen, immutable
    manifest can't carry on its own (status changes after the manifest
    is built; `created_at`/`started_at` are two different instants)."""

    cohort_id: str
    cohort_name: str
    status: CohortStatus
    created_at: datetime
    manifest: StrategyVersionManifest
    started_at: datetime | None = None


@dataclass(frozen=True)
class OpportunityRecord:
    """One fully-decided opportunity: every serious strategy
    alternative the Strategy Competition Engine considered (captured at
    decision time by the caller, before any outcome was known -- see
    `src.validation.counterfactual`'s own module docstring on never
    reconstructing alternatives with hindsight), which one (if any) was
    proposed, and the complete real pipeline outcome for that proposal
    -- the same `QuantitativeAnalysis`/Devil's-Advocate/Portfolio-
    Manager/Risk-Engine/Order/Fidelity-ticket objects
    `src.orchestration.pipeline.PipelineOutcome` already carries,
    reused unmodified rather than re-derived into a second shape."""

    opportunity_id: str
    cohort_id: str
    ticker: str
    created_at: datetime
    cash_no_trade: bool
    alternatives: tuple[StrategyAlternativeRecord, ...]
    proposal_id: str | None = None
    quantitative_analysis: QuantitativeAnalysis | None = None
    # Stores the validated structured output itself
    # (`DevilsAdvocateReview`/`PortfolioDecision`), not the wrapping
    # `DevilsAdvocateEvaluation`/`PortfolioManagerEvaluation` -- those
    # wrappers carry an `AgentCallResult.validated_output: BaseModel`
    # field typed as the generic base class (necessarily, since one
    # wrapper type serves every agent role), which a generic
    # `TypeAdapter` cannot round-trip to its true concrete subtype. The
    # review/decision itself is always a concrete, fully-typed pydantic
    # model and round-trips exactly.
    devils_advocate_review: DevilsAdvocateReview | None = None
    portfolio_manager_decision: PortfolioDecision | None = None
    risk_decision: RiskDecisionResult | None = None
    pipeline_status: str | None = None
