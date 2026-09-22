"""Part 13: the Rolling Engine.

**A roll is NOT one transaction.** It is modeled as exactly two:
CLOSE OLD POSITION (which realizes P&L in full, right then) followed
by OPEN NEW POSITION (a genuinely new `TradeProposal` that must pass
Quant, Devil's Advocate, Portfolio Manager, and Risk exactly like any
other proposal — rolling grants no shortcut through the pipeline; this
module builds only the deterministic bookkeeping tying the two legs
together, it never itself approves anything).

**Rolling never hides a loss.** `RollRecord.total_realized_loss_before_roll`
is the honest, already-realized cumulative P&L of every leg closed so
far in this roll chain — it is never netted against the new position's
unrealized economics to make a losing roll look breakeven, and it is
recorded the moment the old leg closes, before the new leg is even
opened (Part 5's "do not increase position size to recover losses" and
"no automatic rolling to avoid recognizing losses" apply directly
here: a roll that has not yet realized its prior loss in the ledger
would be exactly that).

**`NO_ROLL` is always valid** and requires no call into this module at
all — a position that is simply closed, or simply left to expire, or
simply held, never touches `rolling.py`.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from src.lifecycle.policy import ManagementPolicy


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class RollNotPermittedError(ValueError):
    """Raised when a roll is attempted against a `ManagementPolicy`
    whose `roll_allowed` is `False` -- rolling is opt-in per policy,
    never assumed."""


class RollAlreadyCompletedError(ValueError):
    """Raised by `complete_roll` on a `RollRecord` that already has a
    `roll_to_trade_id` -- a roll leg may only be completed once."""


class RollRecord(BaseModel):
    """One roll: the CLOSE of `roll_from_trade_id`, and — once known —
    the OPEN of `roll_to_trade_id`. `roll_to_trade_id`/`net_roll_credit_debit`
    are `None` between `record_roll_close` and `complete_roll`: the new
    position is a separate proposal that must independently clear the
    full approval pipeline, and may in principle never be opened at all
    (the old position was closed, no roll completed, i.e. this
    degenerates into an ordinary close, still fully and honestly
    recorded)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roll_chain_id: str
    roll_from_trade_id: str
    roll_to_trade_id: str | None = None
    initiated_at: datetime

    realized_pnl_on_close: float
    total_realized_loss_before_roll: float  # cumulative realized P&L across this entire chain, through this leg's close
    close_cash_flow: float  # signed: positive = net credit received closing the old leg, negative = net debit paid
    net_roll_credit_debit: float | None = None  # close_cash_flow + open_cash_flow, set only by complete_roll

    reason: str

    _validate_tz = field_validator("initiated_at")(_tz_aware)


def new_roll_chain_id(seed_trade_id: str) -> str:
    """Deterministic chain id for the FIRST roll of a given original
    position -- every subsequent roll of the same chain reuses this
    same string (passed back in as `roll_chain_id`), never regenerates
    it, so the whole chain stays queryable under one id no matter how
    many times it is rolled."""
    return f"ROLLCHAIN-{seed_trade_id}"


def record_roll_close(
    *,
    policy: ManagementPolicy,
    roll_chain_id: str,
    roll_from_trade_id: str,
    as_of: datetime,
    realized_pnl_on_close: float,
    close_cash_flow: float,
    prior_chain_realized_pnl: float,
    reason: str,
) -> RollRecord:
    """Records the CLOSE half of a roll. `prior_chain_realized_pnl` is
    the cumulative realized P&L of every earlier leg in this same
    `roll_chain_id` (0.0 for the first roll in a chain) — supplied by
    the caller's own persisted roll-chain history, never re-derived or
    guessed here."""
    if not policy.roll_allowed:
        raise RollNotPermittedError(f"{policy.name}: roll_allowed is False -- this policy does not permit rolling")
    return RollRecord(
        roll_chain_id=roll_chain_id,
        roll_from_trade_id=roll_from_trade_id,
        roll_to_trade_id=None,
        initiated_at=as_of,
        realized_pnl_on_close=realized_pnl_on_close,
        total_realized_loss_before_roll=prior_chain_realized_pnl + realized_pnl_on_close,
        close_cash_flow=close_cash_flow,
        net_roll_credit_debit=None,
        reason=reason,
    )


def complete_roll(record: RollRecord, *, roll_to_trade_id: str, open_cash_flow: float) -> RollRecord:
    """Records the OPEN half, once the new position has actually
    cleared the full pipeline (Quant -> Devil's Advocate -> Portfolio
    Manager -> Risk) and been filled. Returns a NEW `RollRecord` —
    never mutates `record`."""
    if record.roll_to_trade_id is not None:
        raise RollAlreadyCompletedError(
            f"roll chain {record.roll_chain_id!r} leg {record.roll_from_trade_id!r} "
            f"was already completed to {record.roll_to_trade_id!r}"
        )
    return record.model_copy(
        update={
            "roll_to_trade_id": roll_to_trade_id,
            "net_roll_credit_debit": record.close_cash_flow + open_cash_flow,
        }
    )
