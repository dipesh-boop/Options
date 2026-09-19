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
class MarketContext:
    """Qualitative/reference market context (index levels, known
    upcoming events). Never a source of new numeric truth for pricing —
    that stays with Python Quant."""

    as_of: datetime
    vix_level: float | None
    notable_events: list[str] = field(default_factory=list)


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
