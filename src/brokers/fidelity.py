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

Every quantitative field on `ApprovedOrder`/`FidelityTradeTicket` is
data this module trusts and displays; it computes none of it (no
dependency on `src.quant`) and has no dependency on `src.llm` at all —
there is no code path by which an LLM's text could reach or alter a
numeric field here. `copy_fidelity_order_text` produces the exact text
a future dashboard's "COPY FIDELITY ORDER" button would copy to the
clipboard; clipboard access itself is a frontend concern
(`navigator.clipboard.writeText` in the browser), not something this
backend function attempts.
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
    # The market moved enough, before a human finished entering the
    # order (or before Fidelity accepted it), that the ticket's
    # limit/minimum-acceptable price is no longer valid and must be
    # regenerated from fresh market data before anyone re-attempts
    # manual entry — never silently resubmitted at the stale price.
    REPRICE_REQUIRED = "reprice_required"


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
    # REJECTED here (Step 18) is distinct from CANCELLED: REJECTED is a
    # human declining an already-approved ticket before ever entering it
    # into Fidelity ("I'm not taking this trade"); CANCELLED (below) is
    # reserved for an order that WAS entered and is now being pulled
    # back. Both are terminal and neither is reachable from the other.
    TicketStatus.AWAITING_HUMAN: frozenset(
        {TicketStatus.ORDER_ENTERED, TicketStatus.REPRICE_REQUIRED, TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED}
    ),
    TicketStatus.ORDER_ENTERED: frozenset(
        {TicketStatus.REPRICE_REQUIRED, TicketStatus.CANCELLED, TicketStatus.EXPIRED}
    ),  # FILLED/PARTIALLY_FILLED: confirm_fill() only
    TicketStatus.PARTIALLY_FILLED: frozenset({TicketStatus.CANCELLED, TicketStatus.EXPIRED}),  # FILLED: confirm_fill() only
    # A repriced ticket must be regenerated (a fresh generate_trade_ticket
    # call from current market data) before a human re-attempts entry —
    # it goes back to AWAITING_HUMAN, never straight to ORDER_ENTERED. A
    # human may also simply decline it outright from here (REJECTED),
    # without first waiting for it to be regenerated.
    TicketStatus.REPRICE_REQUIRED: frozenset(
        {TicketStatus.AWAITING_HUMAN, TicketStatus.CANCELLED, TicketStatus.REJECTED, TicketStatus.EXPIRED}
    ),
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


# ----------------------------------------------------------------------
# Shared validation logic for ApprovedOrder and FidelityTradeTicket.
# The two models intentionally duplicate almost all of their fields
# (ApprovedOrder is what the Risk Engine hands over; FidelityTradeTicket
# is the tracked, stateful artifact with a status/execution_confirmation
# on top) — these module-level functions keep the *validation logic*
# itself in one place rather than duplicating the logic too.
# ----------------------------------------------------------------------


def _check_timezone_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _check_market_data_not_stale(market_data_timestamp: datetime, timestamp: datetime) -> None:
    if market_data_timestamp > timestamp:
        raise ValueError("market_data_timestamp cannot be after timestamp")
    age = timestamp - market_data_timestamp
    if age > MAX_MARKET_DATA_AGE:
        raise ValueError(
            f"market data is stale: {age} old, exceeds max allowed age of {MAX_MARKET_DATA_AGE}. "
            "A ticket must never be generated from stale market data."
        )


def _check_net_bid_ask(net_bid: float, net_ask: float) -> None:
    if net_bid > net_ask:
        raise ValueError(f"net_bid ({net_bid}) cannot exceed net_ask ({net_ask})")


def _check_minimum_acceptable_price(minimum_acceptable_price: float, limit_price: float, estimated_credit_debit: float) -> None:
    if estimated_credit_debit >= 0:
        if minimum_acceptable_price > limit_price:
            raise ValueError("minimum_acceptable_price cannot exceed the target limit_price for a net-credit order")
    else:
        if minimum_acceptable_price < limit_price:
            raise ValueError("minimum_acceptable_price cannot be below the target limit_price for a net-debit order")


def _check_capital_at_risk(capital_at_risk: float, max_loss: float) -> None:
    if capital_at_risk < max_loss:
        raise ValueError(f"capital_at_risk ({capital_at_risk}) cannot be less than max_loss ({max_loss})")


def _check_management_dte(management_dte: int, expiration: date, timestamp: datetime) -> None:
    total_dte = (expiration - timestamp.date()).days
    if management_dte > total_dte:
        raise ValueError(f"management_dte ({management_dte}) cannot exceed the structure's total DTE ({total_dte})")


class ApprovedOrder(StrictModel):
    """What the (not yet implemented) Risk Engine hands to this module.
    Every numeric field here is data this module trusts and displays —
    it computes none of it. `fidelity.py` has no dependency on
    `src.quant`; whatever approved this order already did that math."""

    risk_approval_id: str = Field(min_length=1, max_length=64)
    account_alias: str = Field(min_length=1, max_length=100)
    ticker: str = Field(min_length=1, max_length=10)
    strategy: str = Field(min_length=1, max_length=100)
    underlying_price: float = Field(gt=0)
    expiration: date
    legs: list[FidelityOrderLeg] = Field(min_length=1, max_length=4)
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    minimum_acceptable_price: float = Field(gt=0)
    time_in_force: str = Field(default="DAY", min_length=1, max_length=16)
    estimated_credit_debit: float
    net_bid: float
    net_ask: float
    max_profit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    breakeven: float = Field(gt=0)
    capital_at_risk: float = Field(gt=0)
    return_on_capital: float
    profit_target: float = Field(gt=0)
    loss_management_rule: str = Field(min_length=1, max_length=500)
    DTE_management_rule: str = Field(min_length=1, max_length=500)
    management_dte: int = Field(ge=0, le=365)
    timestamp: datetime
    market_data_timestamp: datetime

    @model_validator(mode="after")
    def _validate(self) -> "ApprovedOrder":
        _check_timezone_aware(self.timestamp, "timestamp")
        _check_timezone_aware(self.market_data_timestamp, "market_data_timestamp")
        _check_market_data_not_stale(self.market_data_timestamp, self.timestamp)
        _check_net_bid_ask(self.net_bid, self.net_ask)
        _check_minimum_acceptable_price(self.minimum_acceptable_price, self.limit_price, self.estimated_credit_debit)
        _check_capital_at_risk(self.capital_at_risk, self.max_loss)
        _check_management_dte(self.management_dte, self.expiration, self.timestamp)
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
    def _validate(self) -> "ExecutionConfirmation":
        _check_timezone_aware(self.confirmed_at, "confirmed_at")
        return self


class FidelityTradeTicket(TimestampedModel):
    """The artifact this module exists to produce. `timestamp`/`source`
    are inherited from `TimestampedModel`; `source` is always
    `"fidelity_manual"` — there is no other kind of Fidelity ticket."""

    trade_id: str = Field(min_length=1, max_length=64)
    account_alias: str = Field(min_length=1, max_length=100)
    ticker: str = Field(min_length=1, max_length=10)
    strategy: str = Field(min_length=1, max_length=100)
    underlying_price: float = Field(gt=0)
    expiration: date
    legs: list[FidelityOrderLeg] = Field(min_length=1, max_length=4)
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    minimum_acceptable_price: float = Field(gt=0)
    time_in_force: str = Field(default="DAY", min_length=1, max_length=16)
    estimated_credit_debit: float
    net_bid: float
    net_ask: float
    max_profit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    breakeven: float = Field(gt=0)
    capital_at_risk: float = Field(gt=0)
    return_on_capital: float
    profit_target: float = Field(gt=0)
    loss_management_rule: str = Field(min_length=1, max_length=500)
    DTE_management_rule: str = Field(min_length=1, max_length=500)
    management_dte: int = Field(ge=0, le=365)
    market_data_timestamp: datetime
    risk_approval_id: str = Field(min_length=1, max_length=64)

    status: TicketStatus = TicketStatus.AWAITING_HUMAN
    status_updated_at: datetime
    execution_confirmation: ExecutionConfirmation | None = None

    @property
    def net_mid(self) -> float:
        return round((self.net_bid + self.net_ask) / 2, 4)

    @model_validator(mode="after")
    def _validate(self) -> "FidelityTradeTicket":
        _check_market_data_not_stale(self.market_data_timestamp, self.timestamp)
        _check_net_bid_ask(self.net_bid, self.net_ask)
        _check_minimum_acceptable_price(self.minimum_acceptable_price, self.limit_price, self.estimated_credit_debit)
        _check_capital_at_risk(self.capital_at_risk, self.max_loss)
        _check_management_dte(self.management_dte, self.expiration, self.timestamp)
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
            account_alias=approved.account_alias,
            ticker=approved.ticker,
            strategy=approved.strategy,
            underlying_price=approved.underlying_price,
            expiration=approved.expiration,
            legs=approved.legs,
            quantity=approved.quantity,
            limit_price=approved.limit_price,
            minimum_acceptable_price=approved.minimum_acceptable_price,
            time_in_force=approved.time_in_force,
            estimated_credit_debit=approved.estimated_credit_debit,
            net_bid=approved.net_bid,
            net_ask=approved.net_ask,
            max_profit=approved.max_profit,
            max_loss=approved.max_loss,
            breakeven=approved.breakeven,
            capital_at_risk=approved.capital_at_risk,
            return_on_capital=approved.return_on_capital,
            profit_target=approved.profit_target,
            loss_management_rule=approved.loss_management_rule,
            DTE_management_rule=approved.DTE_management_rule,
            management_dte=approved.management_dte,
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
    """Formats a ticket to closely match what Fidelity Trader+'s order
    entry screen asks for, so a human can transcribe it field by field.
    Returns a string; it does not print, send, transmit, or copy
    anything to any clipboard — see `copy_fidelity_order_text`."""
    net_label = "CREDIT" if ticket.estimated_credit_debit >= 0 else "DEBIT"

    lines = [
        "ACCOUNT:",
        ticket.account_alias,
        "",
        "UNDERLYING:",
        ticket.ticker,
        "",
        "STRATEGY:",
        ticket.strategy,
        "",
        "EXPIRATION:",
        ticket.expiration.strftime("%m/%d/%Y"),
        "",
    ]

    for i, leg in enumerate(ticket.legs, start=1):
        put_call = "PUT" if leg.put_call == OptionRight.PUT else "CALL"
        lines += [
            f"LEG {i}:",
            _ACTION_LABELS[leg.action],
            ticket.ticker,
            f"{leg.strike:g} {put_call}",
            f"{leg.contracts} contracts",
            "",
        ]

    lines += [
        "ORDER:",
        f"NET {net_label}",
        "",
        "TARGET LIMIT:",
        f"${abs(ticket.limit_price):.2f}",
        "",
        "MINIMUM ACCEPTABLE:",
        f"${abs(ticket.minimum_acceptable_price):.2f}",
        "",
        "TIME IN FORCE:",
        ticket.time_in_force,
        "",
        "CURRENT NET BID:",
        f"${ticket.net_bid:.2f}",
        "",
        "CURRENT NET ASK:",
        f"${ticket.net_ask:.2f}",
        "",
        "CURRENT MID:",
        f"${ticket.net_mid:.2f}",
        "",
        "QUOTE TIME:",
        ticket.market_data_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "",
        "MAX PROFIT:",
        f"${ticket.max_profit:.0f}",
        "",
        "MAX LOSS:",
        f"${ticket.max_loss:.0f}",
        "",
        "BREAKEVEN:",
        f"${ticket.breakeven:.2f}",
        "",
        "CAPITAL AT RISK:",
        f"${ticket.capital_at_risk:.0f}",
        "",
        "RETURN ON CAPITAL:",
        f"{ticket.return_on_capital * 100:.1f}%",
        "",
        "PROFIT TARGET:",
        f"${ticket.profit_target:.2f}",
        "",
        "MANAGEMENT DTE:",
        str(ticket.management_dte),
        "",
        "STATUS:",
        _status_display(ticket.status),
    ]
    return "\n".join(lines)


def copy_fidelity_order_text(ticket: FidelityTradeTicket) -> str:
    """The exact text a future dashboard's "COPY FIDELITY ORDER" button
    puts on the clipboard. Actual clipboard access is a frontend concern
    (the browser's `navigator.clipboard.writeText`, called with the
    string this function returns) — this function has no side effects
    and does not touch any clipboard, OS, or display itself."""
    return render_ticket_text(ticket)


def _status_display(status: TicketStatus) -> str:
    if status == TicketStatus.AWAITING_HUMAN:
        return "AWAITING HUMAN EXECUTION"
    return status.value.upper().replace("_", " ")
