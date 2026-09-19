"""Fidelity manual-execution provider.

Fidelity does not offer a supported public retail API for automated
option/equity order execution. This module does not attempt to work
around that: there is no HTTP client, no browser automation, no scraper,
no credential storage, and — deliberately — **no method anywhere in this
file that can submit an order**. `FidelityManualProvider` converts a
Risk-Engine-approved order into a human-readable `FidelityTradeTicket`
and nothing else. A person reads the ticket and enters it into Fidelity
Trader+ by hand; the system finds out it happened only when that person
(or a future, equally explicit, non-automated confirmation path)
supplies an `ExecutionConfirmation` — never by inferring it.

This module deliberately does NOT implement `src.brokers.base.Broker`.
That interface's contract (`place_order` returns an `Order` with a
`broker_order_id`) describes something that actually submits to a
broker; implementing it here — even as a stub — would misrepresent what
this class can do. `FidelityManualProvider` is not a peer of
`IBKRBroker`; it is a ticket formatter.

Explicitly out of scope, by construction, not by policy alone: reverse-
engineering Fidelity endpoints, scraping Fidelity.com, automating
Fidelity Trader+, browser automation of any kind, storing Fidelity
usernames/passwords, bypassing MFA, using session cookies, or calling
any unofficial API for account control. None of the code below has the
means to do any of these things — see
`tests/unit/brokers/test_fidelity_no_execution.py` for the proof,
including a runtime check that generating a ticket never opens a
network socket.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from enum import Enum
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from src.data.option_chain import OptionRight
from src.data.provider import StrictModel, TimestampedModel

# Same placeholder-policy pattern as src.llm.schemas.MAX_MARKET_DATA_AGE
# and src.data.provider.DEFAULT_MAX_QUOTE_AGE — TODO(Phase 0): one
# config value, not a third independent constant.
MAX_MARKET_DATA_AGE = timedelta(minutes=15)


class ExecutionMode(str, Enum):
    """Deliberately a single-member enum, same pattern as
    `src.brokers.base.BrokerEnvironment`. There is no automated mode to
    accidentally select — adding one would be a real, separate,
    future decision about a completely different kind of integration,
    not a flag on this one."""

    MANUAL_EXECUTION = "manual_execution"


class TicketStatus(str, Enum):
    PROPOSED = "proposed"
    QUANT_APPROVED = "quant_approved"
    LLM_REVIEWED = "llm_reviewed"
    RISK_APPROVED = "risk_approved"
    AWAITING_HUMAN = "awaiting_human"
    ORDER_ENTERED = "order_entered"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_TERMINAL_STATUSES = frozenset(
    {TicketStatus.FILLED, TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED}
)

# The full pipeline lifecycle this ticket moves through, start to finish.
# Risk-approved does NOT mean executed: everything up to and including
# RISK_APPROVED is a Python/agent-layer judgment; AWAITING_HUMAN is where
# this file's job (generate_trade_ticket) ends; ORDER_ENTERED means a
# human says they typed it into Trader+; FILLED/PARTIALLY_FILLED are
# reachable ONLY through confirm_fill() with a real ExecutionConfirmation
# — never through transition() (see _FILL_ONLY_STATUSES below).
_ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.PROPOSED: frozenset({TicketStatus.QUANT_APPROVED, TicketStatus.REJECTED, TicketStatus.EXPIRED}),
    TicketStatus.QUANT_APPROVED: frozenset({TicketStatus.LLM_REVIEWED, TicketStatus.REJECTED, TicketStatus.EXPIRED}),
    TicketStatus.LLM_REVIEWED: frozenset({TicketStatus.RISK_APPROVED, TicketStatus.REJECTED, TicketStatus.EXPIRED}),
    TicketStatus.RISK_APPROVED: frozenset({TicketStatus.AWAITING_HUMAN, TicketStatus.REJECTED, TicketStatus.EXPIRED}),
    TicketStatus.AWAITING_HUMAN: frozenset({TicketStatus.ORDER_ENTERED, TicketStatus.CANCELLED, TicketStatus.EXPIRED}),
    TicketStatus.ORDER_ENTERED: frozenset({TicketStatus.CANCELLED, TicketStatus.EXPIRED}),  # FILLED/PARTIALLY_FILLED: confirm_fill() only
    TicketStatus.PARTIALLY_FILLED: frozenset({TicketStatus.CANCELLED, TicketStatus.EXPIRED}),  # FILLED: confirm_fill() only
    TicketStatus.FILLED: frozenset(),
    TicketStatus.CANCELLED: frozenset(),
    TicketStatus.REJECTED: frozenset(),
    TicketStatus.EXPIRED: frozenset(),
}

_FILL_ONLY_STATUSES = frozenset({TicketStatus.FILLED, TicketStatus.PARTIALLY_FILLED})
_FILL_CONFIRMABLE_FROM = frozenset({TicketStatus.ORDER_ENTERED, TicketStatus.PARTIALLY_FILLED})


class InvalidTransitionError(RuntimeError):
    """Raised for any status change outside the allowed transition
    graph — including, always, an attempt to reach FILLED or
    PARTIALLY_FILLED through `transition()` instead of `confirm_fill()`.
    """


class FidelityLegAction(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"


class FidelityOrderLeg(StrictModel):
    action: FidelityLegAction
    put_call: OptionRight
    strike: float = Field(gt=0)
    expiration: date
    contracts: int = Field(gt=0)


class ApprovedOrder(StrictModel):
    """What the (not yet implemented) Risk Engine hands to this module.
    Every numeric field here is data this module trusts and displays —
    it computes none of it. `fidelity.py` has no dependency on
    `src.quant`; whatever approved this order already did that math."""

    risk_approval_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=10)
    strategy: str = Field(min_length=1, max_length=100)
    underlying_price: float = Field(gt=0)
    expiration: date
    legs: list[FidelityOrderLeg] = Field(min_length=1, max_length=4)
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    estimated_credit_debit: float
    max_profit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    breakeven: float = Field(gt=0)
    return_on_capital: float
    profit_target: float = Field(gt=0)
    loss_management_rule: str = Field(min_length=1, max_length=500)
    DTE_management_rule: str = Field(min_length=1, max_length=500)
    timestamp: datetime
    market_data_timestamp: datetime

    @model_validator(mode="after")
    def _require_timezone_aware(self) -> "ApprovedOrder":
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.market_data_timestamp.tzinfo is None:
            raise ValueError("market_data_timestamp must be timezone-aware")
        return self

    @model_validator(mode="after")
    def _market_data_not_stale(self) -> "ApprovedOrder":
        if self.market_data_timestamp > self.timestamp:
            raise ValueError("market_data_timestamp cannot be after timestamp")
        age = self.timestamp - self.market_data_timestamp
        if age > MAX_MARKET_DATA_AGE:
            raise ValueError(
                f"market data is stale: {age} old, exceeds max allowed age of {MAX_MARKET_DATA_AGE}. "
                "A ticket must never be generated from stale market data."
            )
        return self


class ExecutionConfirmation(StrictModel):
    """Explicit, external evidence that a fill actually happened. This
    is the ONLY input that can move a ticket to FILLED or
    PARTIALLY_FILLED — there is no other path (see `confirm_fill`)."""

    confirmed_by: str = Field(min_length=1, max_length=100)
    confirmation_source: Literal["human_manual_entry", "broker_confirmation_feed"]
    filled_quantity: int = Field(gt=0)
    fill_price: float = Field(gt=0)
    confirmed_at: datetime

    @model_validator(mode="after")
    def _require_timezone_aware(self) -> "ExecutionConfirmation":
        if self.confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        return self


class FidelityTradeTicket(TimestampedModel):
    """The artifact this module exists to produce. `timestamp`/`source`
    are inherited from `TimestampedModel`; `source` is always
    `"fidelity_manual"` — there is no other kind of Fidelity ticket."""

    trade_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=10)
    strategy: str = Field(min_length=1, max_length=100)
    underlying_price: float = Field(gt=0)
    expiration: date
    legs: list[FidelityOrderLeg] = Field(min_length=1, max_length=4)
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    estimated_credit_debit: float
    max_profit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    breakeven: float = Field(gt=0)
    return_on_capital: float
    profit_target: float = Field(gt=0)
    loss_management_rule: str = Field(min_length=1, max_length=500)
    DTE_management_rule: str = Field(min_length=1, max_length=500)
    market_data_timestamp: datetime
    risk_approval_id: str = Field(min_length=1, max_length=64)

    status: TicketStatus = TicketStatus.AWAITING_HUMAN
    status_updated_at: datetime
    execution_confirmation: ExecutionConfirmation | None = None

    @model_validator(mode="after")
    def _market_data_not_stale(self) -> "FidelityTradeTicket":
        if self.market_data_timestamp > self.timestamp:
            raise ValueError("market_data_timestamp cannot be after timestamp")
        age = self.timestamp - self.market_data_timestamp
        if age > MAX_MARKET_DATA_AGE:
            raise ValueError(
                f"market data is stale: {age} old, exceeds max allowed age of {MAX_MARKET_DATA_AGE}. "
                "A ticket must never be generated from stale market data."
            )
        return self

    @model_validator(mode="after")
    def _execution_confirmation_only_when_filled(self) -> "FidelityTradeTicket":
        has_confirmation = self.execution_confirmation is not None
        is_fill_status = self.status in _FILL_ONLY_STATUSES
        if has_confirmation != is_fill_status:
            raise ValueError(
                "execution_confirmation must be present if and only if status is FILLED or "
                "PARTIALLY_FILLED — risk-approved does not mean executed, and a fill status "
                "without evidence (or evidence without a fill status) is a contradiction."
            )
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def transition(ticket: FidelityTradeTicket, new_status: TicketStatus, *, at: datetime) -> FidelityTradeTicket:
    """Every status change except reaching FILLED/PARTIALLY_FILLED.
    Raises InvalidTransitionError for anything not in the allowed
    transition graph — including any attempt to reach a fill status this
    way; that path is confirm_fill() only, and only from there."""
    if new_status in _FILL_ONLY_STATUSES:
        raise InvalidTransitionError(
            f"{new_status.value} cannot be reached via transition() — it requires an explicit "
            "ExecutionConfirmation via confirm_fill()."
        )
    allowed = _ALLOWED_TRANSITIONS.get(ticket.status, frozenset())
    if new_status not in allowed:
        raise InvalidTransitionError(f"cannot transition from {ticket.status.value!r} to {new_status.value!r}")
    return ticket.model_copy(update={"status": new_status, "status_updated_at": at})


def confirm_fill(ticket: FidelityTradeTicket, confirmation: ExecutionConfirmation) -> FidelityTradeTicket:
    """The ONLY function in this codebase that can produce a FILLED or
    PARTIALLY_FILLED ticket, and only from ORDER_ENTERED (a human has
    already said they entered it) or PARTIALLY_FILLED (a further fill on
    an already-partial position). Requires a real
    `ExecutionConfirmation` — there is no way to call this without
    supplying one, and no other function reaches these statuses at all."""
    if ticket.status not in _FILL_CONFIRMABLE_FROM:
        raise InvalidTransitionError(
            f"cannot confirm a fill from status {ticket.status.value!r} — a ticket must be "
            "ORDER_ENTERED (a human has confirmed manual entry) first."
        )
    new_status = TicketStatus.FILLED if confirmation.filled_quantity >= ticket.quantity else TicketStatus.PARTIALLY_FILLED
    return ticket.model_copy(
        update={
            "status": new_status,
            "status_updated_at": confirmation.confirmed_at,
            "execution_confirmation": confirmation,
        }
    )


class FidelityManualProvider:
    """Converts a Risk-Engine-approved order into a `FidelityTradeTicket`
    for a human to manually enter into Fidelity Trader+. This is the
    entire capability of this class — there is no `place_order`, no
    `submit_order`, no `send_order`, no network client of any kind
    anywhere on it or imported by this module."""

    execution_mode: ClassVar[ExecutionMode] = ExecutionMode.MANUAL_EXECUTION

    def generate_trade_ticket(
        self,
        approved: ApprovedOrder,
        *,
        trade_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> FidelityTradeTicket:
        now = generated_at or approved.timestamp
        return FidelityTradeTicket(
            trade_id=trade_id or str(uuid.uuid4()),
            ticker=approved.ticker,
            strategy=approved.strategy,
            underlying_price=approved.underlying_price,
            expiration=approved.expiration,
            legs=approved.legs,
            quantity=approved.quantity,
            limit_price=approved.limit_price,
            estimated_credit_debit=approved.estimated_credit_debit,
            max_profit=approved.max_profit,
            max_loss=approved.max_loss,
            breakeven=approved.breakeven,
            return_on_capital=approved.return_on_capital,
            profit_target=approved.profit_target,
            loss_management_rule=approved.loss_management_rule,
            DTE_management_rule=approved.DTE_management_rule,
            market_data_timestamp=approved.market_data_timestamp,
            risk_approval_id=approved.risk_approval_id,
            status=TicketStatus.AWAITING_HUMAN,
            status_updated_at=now,
            timestamp=now,
            source="fidelity_manual",
        )


_ACTION_LABELS = {
    FidelityLegAction.BUY_TO_OPEN: "BUY TO OPEN",
    FidelityLegAction.SELL_TO_OPEN: "SELL TO OPEN",
    FidelityLegAction.BUY_TO_CLOSE: "BUY TO CLOSE",
    FidelityLegAction.SELL_TO_CLOSE: "SELL TO CLOSE",
}


def render_ticket_text(ticket: FidelityTradeTicket) -> str:
    """Formats a ticket as plain text for a human to read before typing
    the order into Fidelity Trader+ themselves — this function returns a
    string; it does not print, send, or transmit anything."""
    lines = [
        "FIDELITY TRADE TICKET",
        "",
        f"{ticket.ticker} {ticket.strategy}",
        "",
        "Expiration:",
        ticket.expiration.strftime("%m/%d/%Y"),
        "",
    ]
    for leg in ticket.legs:
        put_call = "PUT" if leg.put_call == OptionRight.PUT else "CALL"
        lines.append(f"{_ACTION_LABELS[leg.action]}:")
        lines.append(f"{leg.contracts} {ticket.ticker} {leg.strike:g} {put_call}")
        lines.append("")

    net = "CREDIT" if ticket.estimated_credit_debit >= 0 else "DEBIT"
    lines += [
        "ORDER TYPE:",
        f"NET {net} LIMIT",
        "",
        f"TARGET {net}:",
        f"${abs(ticket.limit_price):.2f}",
        "",
        "MAXIMUM PROFIT:",
        f"${ticket.max_profit:.0f}",
        "",
        "MAXIMUM LOSS:",
        f"${ticket.max_loss:.0f}",
        "",
        "BREAKEVEN:",
        f"${ticket.breakeven:.2f}",
        "",
        "PROFIT TARGET:",
        f"${ticket.profit_target:.2f}",
        "",
        "LOSS MANAGEMENT:",
        ticket.loss_management_rule,
        "",
        "DTE MANAGEMENT:",
        ticket.DTE_management_rule,
        "",
        "STATUS:",
        _status_display(ticket.status),
    ]
    return "\n".join(lines)


def _status_display(status: TicketStatus) -> str:
    if status == TicketStatus.AWAITING_HUMAN:
        return "AWAITING HUMAN EXECUTION"
    return status.value.upper().replace("_", " ")
