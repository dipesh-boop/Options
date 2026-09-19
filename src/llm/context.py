"""Read-only context builders for the LLM orchestration layer.

Every type here represents pre-computed, Python-verified data the agent
layer is allowed to see (ARCHITECTURE.md §2, §5). No builder in this
module invents a number; each one only serializes data handed to it by
the caller — the eventual deterministic screener/quant/risk-engine layer
(not implemented yet), or a test/mock in the meantime. Output is always
plain JSON, ready to drop into a prompt's user content.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class CandidateContext:
    """One screener-approved candidate, as the agent layer is allowed to
    see it. Every numeric field here was computed by Python, never the
    LLM, and is presented purely as read-only reference data."""

    symbol: str
    strategy_type: str
    expiry: date
    strike: float
    right: str
    mid_price: float
    implied_vol: float
    delta: float
    open_interest: int
    volume: int


@dataclass(frozen=True)
class PortfolioStateContext:
    """Current portfolio snapshot, as computed by Python Risk Engine
    (not implemented yet — see IMPLEMENTATION_PLAN.md Phase 1). Read-only
    to every agent role; no agent output is ever allowed to alter it
    directly."""

    nav: float
    net_delta: float
    net_theta: float
    net_vega: float
    current_drawdown_pct: float
    open_position_count: int
    positions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class QuantitativeAnalysisContext:
    """Python Quant's computed economics for one proposed structure —
    mirrors `src.quant.expected_value.StrategyEconomics` field for field,
    as a plain dataclass rather than an import, so `src.llm` stays fully
    decoupled from `src.quant` (same reason `CandidateContext` doesn't
    import `src.data.option_chain.OptionContract`). Every field here was
    computed by Python; the Portfolio Manager may read and reference
    these numbers in its narrative but must never restate a different
    one — see `PortfolioDecision`'s module docstring."""

    max_profit: float
    max_loss: float
    breakeven: float
    capital_required: float
    return_on_capital: float
    annualized_roc: float
    probability_of_profit: float
    expected_value: float


@dataclass(frozen=True)
class RiskEngineContext:
    """Python Risk Engine's already-reached decision on one proposal —
    mirrors `src.risk.engine.RiskDecisionResult`'s key fields as a plain
    dataclass, for the same decoupling reason as
    `QuantitativeAnalysisContext` above. The Portfolio Manager sees this
    as a fact to narrate around, never a recommendation it can revise:
    it has no authority to override what this record says, and no field
    anywhere in its output schema (`PortfolioDecision`) through which it
    could even try."""

    decision: str  # RiskDecision.value: "approve" | "resize" | "reject" | "halt"
    reason_codes: list[str]
    approved_contracts: int | None
    message: str


@dataclass(frozen=True)
class MarketSnapshotContext:
    """A point-in-time options quote snapshot — mirrors the relevant
    fields of `src.data.option_chain.OptionContract` as a plain
    dataclass, for the same decoupling reason as
    `QuantitativeAnalysisContext`. `src.llm.devils_advocate` uses two of
    these (one from proposal/analysis time, one current) to deterministically
    compute how much conditions have moved between analysis and a human
    actually entering the order in Fidelity Trader+ — the comparison
    itself is Python arithmetic, never left to the model to estimate."""

    as_of: datetime
    underlying_price: float
    bid: float
    ask: float
    iv: float | None
    delta: float | None


@dataclass(frozen=True)
class MarketContext:
    """Qualitative/reference market context (index levels, known
    upcoming events). Never a source of new numeric truth for pricing —
    that stays with Python Quant."""

    as_of: datetime
    vix_level: float | None
    notable_events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PerformanceBreakdownContext:
    """One dimension's worth of bucketed backtest performance — mirrors
    `src.research.performance_breakdown.PerformanceBreakdownReport`
    field for field, as a plain dataclass, for the same decoupling
    reason `QuantitativeAnalysisContext` doesn't import `src.quant`.
    Every figure here was computed by Python from already-closed
    backtest trades; the Strategy Research Agent may narrate around
    these numbers but must never restate a different one."""

    dimension: str
    buckets: list[dict[str, Any]]
    total_trades: int


@dataclass(frozen=True)
class OverfittingGuardContext:
    """Mirrors `src.research.overfitting_guards.OverfittingGuardResult`.
    `warnings` always includes the standing survivorship-bias caution —
    this agent may discuss these warnings but cannot silence, edit, or
    add to the list Python computed."""

    hypotheses_tested: int
    hypotheses_rejected: int
    surviving_validation: int
    surviving_out_of_sample: int
    warnings: list[str]


@dataclass(frozen=True)
class FidelityPracticalityContext:
    """Mirrors `src.research.fidelity_practicality.FidelityPracticalityResult`.
    `rating` is Python's own classification — `src.llm.strategy_research`
    overwrites the model's own `StrategyResearchReview.fidelity_practicality_rating`
    with this value if the two ever disagree, the same "Python overrides
    the LLM, never the reverse" relationship every other classification
    in this codebase has with its agent layer."""

    rating: str
    burden_score: int
    reasons: list[str]
    hard_rejected: bool


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def build_agent_context(
    *,
    candidates: list[CandidateContext] | None = None,
    portfolio: PortfolioStateContext | None = None,
    market: MarketContext | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Serialize whatever subset of context a given agent role needs into
    a single JSON string for the prompt's user content. Callers pass only
    what that role is allowed to see (e.g. the Market Agent gets `market`
    but not `portfolio`); omitted sections are simply absent from the
    payload rather than sent as null placeholders."""

    payload: dict[str, Any] = {}
    if candidates is not None:
        payload["candidates"] = [vars(c) for c in candidates]
    if portfolio is not None:
        payload["portfolio"] = vars(portfolio)
    if market is not None:
        payload["market"] = vars(market)
    if extra:
        payload["extra"] = extra
    return json.dumps(payload, default=_json_default, indent=2, sort_keys=True)
