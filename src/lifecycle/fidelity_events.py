"""Part 21: Fidelity manual-execution wiring for lifecycle-driven
actions (close, partial close, roll-close, adjustment-close).

**Risk approval is not execution; a human still manually executes
every leg through Fidelity Trader+.** This module produces
`LifecycleClosingTicket`s — plain, immutable, human-readable
instructions for a CLOSE POSITION / BUY TO CLOSE / SELL TO CLOSE leg —
and nothing else. It deliberately does NOT reuse `ApprovedOrder`/
`FidelityTradeTicket` (`src.brokers.fidelity`): that schema requires
`max_profit`/`max_loss`/`breakeven`, fields that only have real meaning
for an OPENING trade. Forcing a fabricated number onto a closing ticket
to satisfy that schema would be exactly the kind of invented figure
CLAUDE.md forbids — this is the same, pre-existing, documented boundary
`src.wheel.fidelity_events` describes for a Wheel's own discretionary
buy-to-close.

**A roll's OPEN leg is a genuinely new `TradeProposal`** and gets a
real `FidelityTradeTicket` the ordinary way, through the unmodified
`src.risk.engine`/`FidelityManualProvider` pipeline — this module only
ever covers the CLOSE side of a close/roll/adjustment.

**`FILLED` still requires an explicit human confirmation.** Exactly
like every other Fidelity ticket in this platform, a
`LifecycleClosingTicket` never marks itself filled from a `PaperBroker`
fill or any other automatic signal — only `record_ticket_filled`,
called with a human's own reported fill facts, moves it there.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from src.data.option_chain import OptionRight


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class LifecycleTicketAlreadyFinalizedError(ValueError):
    """Raised by `record_ticket_filled`/`record_ticket_cancelled` on a
    ticket that is already `FILLED` or `CANCELLED` — a ticket's
    terminal outcome, once recorded, is never overwritten."""


class LifecycleClosingAction(str, Enum):
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"


class LifecycleTicketStatus(str, Enum):
    AWAITING_HUMAN = "awaiting_human"
    FILLED = "filled"
    CANCELLED = "cancelled"


class LifecycleClosingLegTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: LifecycleClosingAction
    put_call: OptionRight
    strike: float
    expiration: date
    contracts: int


class LifecycleClosingTicket(BaseModel):
    """One human-readable manual-execution instruction for closing (or
    partially closing) an existing position — including the CLOSE half
    of a roll (`roll_chain_id` set) or an adjustment. Never
    auto-fillable: `status` only ever moves forward via
    `record_ticket_filled`/`record_ticket_cancelled`, called from a
    human's own explicit confirmation, exactly like
    `FidelityTradeTicket.confirm_fill` elsewhere in this platform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: str
    trade_id: str
    roll_chain_id: str | None = None
    ticker: str
    legs: tuple[LifecycleClosingLegTicket, ...]
    reason: str
    created_at: datetime
    status: LifecycleTicketStatus = LifecycleTicketStatus.AWAITING_HUMAN
    filled_at: datetime | None = None
    fill_notes: str | None = None

    _validate_tz = field_validator("created_at")(_tz_aware)


def build_closing_ticket(
    *,
    trade_id: str,
    ticker: str,
    legs: list[LifecycleClosingLegTicket],
    reason: str,
    now: datetime,
    roll_chain_id: str | None = None,
) -> LifecycleClosingTicket:
    return LifecycleClosingTicket(
        ticket_id=f"LCT-{uuid.uuid4().hex[:20]}",
        trade_id=trade_id,
        roll_chain_id=roll_chain_id,
        ticker=ticker,
        legs=tuple(legs),
        reason=reason,
        created_at=now,
    )


def render_closing_ticket_text(ticket: LifecycleClosingTicket) -> str:
    """Plain text for a human to read and manually type into Fidelity
    Trader+ — never anything this platform submits itself."""
    lines = [
        "MANUAL EXECUTION REQUIRED -- read and enter in Fidelity Trader+ yourself.",
        "No order has been submitted to any brokerage by this platform.",
        "",
        f"TICKER: {ticket.ticker}",
        f"REASON: {ticket.reason}",
    ]
    if ticket.roll_chain_id is not None:
        lines.append(f"ROLL CHAIN: {ticket.roll_chain_id}")
    lines.append("")
    for leg in ticket.legs:
        action_text = leg.action.value.upper().replace("_", " ")
        lines.append(
            f"{action_text}: {leg.contracts} x {ticket.ticker} {leg.strike:.2f} {leg.put_call.value.upper()} "
            f"exp {leg.expiration.isoformat()}"
        )
    return "\n".join(lines)


def record_ticket_filled(ticket: LifecycleClosingTicket, *, filled_at: datetime, fill_notes: str | None = None) -> LifecycleClosingTicket:
    if ticket.status != LifecycleTicketStatus.AWAITING_HUMAN:
        raise LifecycleTicketAlreadyFinalizedError(f"ticket {ticket.ticket_id!r} is already {ticket.status.value!r}")
    return ticket.model_copy(update={"status": LifecycleTicketStatus.FILLED, "filled_at": filled_at, "fill_notes": fill_notes})


def record_ticket_cancelled(ticket: LifecycleClosingTicket) -> LifecycleClosingTicket:
    if ticket.status != LifecycleTicketStatus.AWAITING_HUMAN:
        raise LifecycleTicketAlreadyFinalizedError(f"ticket {ticket.ticket_id!r} is already {ticket.status.value!r}")
    return ticket.model_copy(update={"status": LifecycleTicketStatus.CANCELLED})
