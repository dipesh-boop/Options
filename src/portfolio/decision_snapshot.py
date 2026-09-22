"""Part 30: the immutable `PortfolioControlDecisionSnapshot`.

One record per decision point the control loop touches in a cycle --
either an existing open position (most fields populated from
`src.portfolio.revaluation`/`src.lifecycle.engine`) or a new-opportunity
candidate (`position_id` left `None`, `opportunity_proposal_id`/
`new_opportunity_comparison` populated instead). Exactly like
`src.lifecycle.snapshot.LifecycleDecisionSnapshot`, this is append-only
and never mutated after creation -- `src.portfolio.persistence` stores
every one produced, forever, so a future 90-day-validation reviewer (or
a human today) can reconstruct exactly why the control loop recommended
what it recommended, without re-deriving it from raw market data that
may no longer even be available.

This module computes nothing. Every field is either copied verbatim
from an object `src.quant`/`src.risk`/`src.lifecycle`/
`src.portfolio.revaluation` already produced, or is the
`ControlLoopAction`/reason-code label the caller has already decided on
-- the same "read-only context, never a fabricated number" discipline
CLAUDE.md requires of every LLM-facing schema in this codebase, applied
here to the control loop's own audit record.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from src.portfolio.actions import ControlLoopAction


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class PortfolioControlDecisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle_id: str
    timestamp: datetime

    is_trading_day: bool
    is_market_open: bool

    provider: str
    provider_health_status: str
    market_data_timestamp: datetime | None = None

    portfolio_nav: float
    portfolio_cash: float
    portfolio_deployed_pct: float
    portfolio_drawdown_pct: float
    portfolio_delta: float | None = None
    portfolio_gamma: float | None = None
    portfolio_theta: float | None = None
    portfolio_vega: float | None = None

    # Populated for an existing-position decision; left None for a
    # new-opportunity-only decision point.
    position_id: str | None = None
    strategy: str | None = None
    lifecycle_state: str | None = None
    management_policy: str | None = None
    underlying_price: float | None = None
    unrealized_pnl: float | None = None
    dte: int | None = None

    trigger_reason_codes: tuple[str, ...] = ()

    risk_decision: str | None = None
    risk_reason: str | None = None

    recommended_action: ControlLoopAction
    reason_codes: tuple[str, ...] = ()
    alternative_considered: str | None = None

    # Populated when this decision point involved comparing a new
    # candidate against the current portfolio (Part 24) -- the full
    # structured comparison lives wherever `src.portfolio.opportunity_scan`
    # persists it; this snapshot keeps only the reference id and a short
    # human-readable summary, exactly like `LifecycleDecisionSnapshot`
    # keeps `management_policy` as a name, not the full policy object.
    opportunity_proposal_id: str | None = None
    new_opportunity_comparison: str | None = None

    llm_commentary: str | None = None
    human_action_required: bool = False

    # Populated only after `src.brokers.fidelity.confirm_fill`/
    # `src.brokers.paper.PaperBroker` actually records a fill referencing
    # the ticket/order this recommendation led to -- never set by this
    # snapshot's own creation (Part 22: "Risk approval != fill").
    fill_reference: str | None = None

    _validate_timestamp = field_validator("timestamp")(_tz_aware)

    @field_validator("market_data_timestamp")
    @classmethod
    def _validate_market_data_timestamp(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None
