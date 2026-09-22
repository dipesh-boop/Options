"""Part 14: the Adjustment Engine.

**An adjustment must never be improvised by an LLM.** This module
defines a small, closed set of named, explicitly-supported
transformations (`AdjustmentType`) — every one of them carries a
deterministic before/after exposure, incremental cash flow, new max
loss, new capital requirement, and (where applicable) new breakevens
and Greeks. **This module never computes those numbers itself** — like
`src.lifecycle.rolling`, it packages numbers `src.quant`/`src.risk`
already computed once into an auditable `AdjustmentProposal`; it never
re-derives option math. Producing an `AdjustmentProposal` is not
approval: exactly like a rolled-to position, the resulting portfolio
must still be evaluated by `src.risk.engine.evaluate_trade_proposal`
*before* the adjustment is acted on (Part 14: "Risk must evaluate the
resulting portfolio BEFORE approval").

The canonical worked example from Part 14 — an Iron Condor's
unchallenged side being closed while the challenged side remains open
— is `AdjustmentType.CLOSE_UNCHALLENGED_SIDE`; nothing about this
module is Iron-Condor-specific, so the same type applies to any
multi-leg structure with an unchallenged side (e.g. a Short Iron
Butterfly, a Long Strangle where one side has gone deep OTM and no
longer earns its capital).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from src.lifecycle.policy import ManagementPolicy


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class AdjustmentNotPermittedError(ValueError):
    """Raised when an adjustment is attempted against a
    `ManagementPolicy` whose `adjustment_allowed` is `False`."""


class AdjustmentType(str, Enum):
    REDUCE_SIZE = "reduce_size"
    CLOSE_CHALLENGED_SIDE = "close_challenged_side"
    CLOSE_UNCHALLENGED_SIDE = "close_unchallenged_side"
    CONVERT_STRUCTURE = "convert_structure"
    ADD_DEFINED_RISK_HEDGE = "add_defined_risk_hedge"
    ROLL_STRIKE = "roll_strike"
    ROLL_EXPIRATION = "roll_expiration"


class AdjustmentProposal(BaseModel):
    """One deterministic, fully-costed adjustment candidate. Still
    subject to Risk approval — this object records what the
    transformation IS and what it costs/changes, not that it has been
    approved or executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_id: str
    adjustment_type: AdjustmentType
    as_of: datetime
    reason: str

    incremental_cash_flow: float  # signed: positive = net credit received performing the adjustment itself

    before_max_loss: float
    after_max_loss: float
    before_capital_requirement: float
    after_capital_requirement: float

    before_breakeven: float | None = None
    after_breakeven: float | None = None
    before_breakeven_upper: float | None = None
    after_breakeven_upper: float | None = None

    before_delta: float | None = None
    after_delta: float | None = None
    before_gamma: float | None = None
    after_gamma: float | None = None
    before_theta: float | None = None
    after_theta: float | None = None
    before_vega: float | None = None
    after_vega: float | None = None

    _validate_tz = field_validator("as_of")(_tz_aware)

    @field_validator("before_max_loss", "after_max_loss", "before_capital_requirement", "after_capital_requirement")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"must be non-negative, got {v!r}")
        return v


def propose_adjustment(
    policy: ManagementPolicy,
    *,
    trade_id: str,
    adjustment_type: AdjustmentType,
    as_of: datetime,
    reason: str,
    incremental_cash_flow: float,
    before_max_loss: float,
    after_max_loss: float,
    before_capital_requirement: float,
    after_capital_requirement: float,
    before_breakeven: float | None = None,
    after_breakeven: float | None = None,
    before_breakeven_upper: float | None = None,
    after_breakeven_upper: float | None = None,
    before_delta: float | None = None,
    after_delta: float | None = None,
    before_gamma: float | None = None,
    after_gamma: float | None = None,
    before_theta: float | None = None,
    after_theta: float | None = None,
    before_vega: float | None = None,
    after_vega: float | None = None,
) -> AdjustmentProposal:
    """Builds one `AdjustmentProposal`. Raises `AdjustmentNotPermittedError`
    if `policy.adjustment_allowed` is `False` — adjustments, like rolls,
    are opt-in per policy, never assumed available."""
    if not policy.adjustment_allowed:
        raise AdjustmentNotPermittedError(f"{policy.name}: adjustment_allowed is False -- this policy does not permit adjustments")
    return AdjustmentProposal(
        trade_id=trade_id,
        adjustment_type=adjustment_type,
        as_of=as_of,
        reason=reason,
        incremental_cash_flow=incremental_cash_flow,
        before_max_loss=before_max_loss,
        after_max_loss=after_max_loss,
        before_capital_requirement=before_capital_requirement,
        after_capital_requirement=after_capital_requirement,
        before_breakeven=before_breakeven,
        after_breakeven=after_breakeven,
        before_breakeven_upper=before_breakeven_upper,
        after_breakeven_upper=after_breakeven_upper,
        before_delta=before_delta,
        after_delta=after_delta,
        before_gamma=before_gamma,
        after_gamma=after_gamma,
        before_theta=before_theta,
        after_theta=after_theta,
        before_vega=before_vega,
        after_vega=after_vega,
    )
