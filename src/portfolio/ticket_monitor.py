"""Parts 21-22: pending Fidelity ticket monitoring and fill
reconciliation wiring.

**This module rebuilds nothing.** Fill reconciliation (Part 22) is
already complete: `src.brokers.fidelity.confirm_fill`/`transition` are
the sole state-changing functions for a `FidelityTradeTicket`, and
`src.workflows.reconciliation.reconcile_portfolio` already compares a
human's confirmed Fidelity positions against the internal `Portfolio`
view. This module's actual job is Part 21's own, narrower gap: a
control-loop cycle must watch every ticket still sitting in
`AWAITING_HUMAN`/`ORDER_ENTERED` (approved but not yet filled) and
decide, from fresh market data, whether that ticket's price is still
good -- a stale approved ticket must never remain indefinitely
actionable. When it isn't, this module calls the EXISTING `transition()`
function to move it to `REPRICE_REQUIRED`; it never re-derives a new
price itself (regenerating the ticket from a fresh `ApprovedOrder` is a
separate, later Risk-Engine-approved step, exactly like the first ticket
was built).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.brokers.fidelity import (
    MAX_MARKET_DATA_AGE,
    ExecutionConfirmation,
    FidelityTradeTicket,
    TicketStatus,
    confirm_fill,
    transition,
)

# States in which a ticket is "pending" in Part 21's sense: approved and
# actionable by a human, but not yet filled, cancelled, rejected, or
# expired. `REPRICE_REQUIRED` itself is deliberately excluded -- a
# ticket already flagged stale doesn't need to be flagged again every
# cycle until a fresh ticket replaces it (AWAITING_HUMAN, per
# `transition`'s own graph).
PENDING_TICKET_STATUSES = frozenset({TicketStatus.AWAITING_HUMAN, TicketStatus.ORDER_ENTERED})

DEFAULT_MAX_PRICE_DRIFT_PCT = 0.10


class TicketMonitorAction(str, Enum):
    OK = "ok"
    STALE_MARKET_DATA = "stale_market_data"
    PRICE_DRIFT = "price_drift"
    TARGET_UNREACHABLE = "target_unreachable"
    NO_CURRENT_QUOTE = "no_current_quote"


@dataclass(frozen=True)
class TicketMonitorFinding:
    trade_id: str
    ticket_status: TicketStatus
    action: TicketMonitorAction
    reason: str
    quote_age: timedelta | None
    current_net_mid: float | None
    original_limit_price: float
    price_drift_pct: float | None


def evaluate_pending_ticket(
    ticket: FidelityTradeTicket,
    *,
    as_of: datetime,
    current_net_bid: float | None,
    current_net_ask: float | None,
    current_market_data_timestamp: datetime | None,
    max_quote_age: timedelta = MAX_MARKET_DATA_AGE,
    max_price_drift_pct: float = DEFAULT_MAX_PRICE_DRIFT_PCT,
) -> TicketMonitorFinding:
    """One pending ticket's own staleness/repricing check -- pure,
    never mutates or transitions the ticket itself (see
    `apply_ticket_monitor_findings` for that). `current_net_bid`/
    `current_net_ask`/`current_market_data_timestamp` are supplied by
    the caller from this cycle's already-quality-gated market data,
    matching every other Part 21/12 phase's "already fetched, this
    module does no I/O" convention."""
    if current_market_data_timestamp is None or current_net_bid is None or current_net_ask is None:
        return TicketMonitorFinding(
            trade_id=ticket.trade_id, ticket_status=ticket.status, action=TicketMonitorAction.NO_CURRENT_QUOTE,
            reason="no current market quote available for this ticket's legs",
            quote_age=None, current_net_mid=None, original_limit_price=ticket.limit_price, price_drift_pct=None,
        )

    quote_age = as_of - current_market_data_timestamp
    if quote_age > max_quote_age:
        return TicketMonitorFinding(
            trade_id=ticket.trade_id, ticket_status=ticket.status, action=TicketMonitorAction.STALE_MARKET_DATA,
            reason=f"current quote is {quote_age} old, exceeding max allowed age {max_quote_age}",
            quote_age=quote_age, current_net_mid=None, original_limit_price=ticket.limit_price, price_drift_pct=None,
        )

    current_net_mid = round((current_net_bid + current_net_ask) / 2, 4)

    # Same net-credit/net-debit sign convention
    # `src.brokers.fidelity._check_minimum_acceptable_price` already
    # uses: a net-credit ticket's achievable price is bounded below by
    # what the market will now actually pay (current_net_bid); a
    # net-debit ticket's achievable price is bounded above by what the
    # market now actually costs (current_net_ask).
    if ticket.estimated_credit_debit >= 0:
        achievable = current_net_bid
        unreachable = achievable < ticket.minimum_acceptable_price
    else:
        achievable = current_net_ask
        unreachable = achievable > ticket.minimum_acceptable_price

    if unreachable:
        return TicketMonitorFinding(
            trade_id=ticket.trade_id, ticket_status=ticket.status, action=TicketMonitorAction.TARGET_UNREACHABLE,
            reason=f"current achievable net price {achievable} no longer satisfies minimum_acceptable_price "
            f"{ticket.minimum_acceptable_price}",
            quote_age=quote_age, current_net_mid=current_net_mid, original_limit_price=ticket.limit_price,
            price_drift_pct=None,
        )

    price_drift_pct = abs(current_net_mid - ticket.limit_price) / abs(ticket.limit_price)
    if price_drift_pct > max_price_drift_pct:
        return TicketMonitorFinding(
            trade_id=ticket.trade_id, ticket_status=ticket.status, action=TicketMonitorAction.PRICE_DRIFT,
            reason=f"current net mid {current_net_mid} has drifted {price_drift_pct:.1%} from the ticket's "
            f"limit price {ticket.limit_price}, exceeding the {max_price_drift_pct:.1%} threshold",
            quote_age=quote_age, current_net_mid=current_net_mid, original_limit_price=ticket.limit_price,
            price_drift_pct=price_drift_pct,
        )

    return TicketMonitorFinding(
        trade_id=ticket.trade_id, ticket_status=ticket.status, action=TicketMonitorAction.OK,
        reason="current market still supports this ticket's price",
        quote_age=quote_age, current_net_mid=current_net_mid, original_limit_price=ticket.limit_price,
        price_drift_pct=price_drift_pct,
    )


def apply_ticket_monitor_finding(
    ticket: FidelityTradeTicket, finding: TicketMonitorFinding, *, at: datetime
) -> FidelityTradeTicket:
    """Transitions `ticket` to `REPRICE_REQUIRED` via the EXISTING
    `transition()` function when `finding.action != OK`; returns
    `ticket` unchanged otherwise. Never called for a ticket not in
    `PENDING_TICKET_STATUSES` -- `transition()`'s own graph would
    reject it anyway, but the caller (`monitor_pending_tickets`) never
    evaluates a non-pending ticket in the first place."""
    if finding.action == TicketMonitorAction.OK:
        return ticket
    return transition(ticket, TicketStatus.REPRICE_REQUIRED, at=at)


@dataclass(frozen=True)
class TicketMonitorResult:
    findings: tuple[TicketMonitorFinding, ...]
    repriced_tickets: tuple[FidelityTradeTicket, ...]

    @property
    def reprice_required_count(self) -> int:
        return len(self.repriced_tickets)


def monitor_pending_tickets(
    tickets: list[FidelityTradeTicket],
    *,
    as_of: datetime,
    current_quotes_by_trade_id: dict[str, tuple[float, float, datetime]],
    max_quote_age: timedelta = MAX_MARKET_DATA_AGE,
    max_price_drift_pct: float = DEFAULT_MAX_PRICE_DRIFT_PCT,
) -> TicketMonitorResult:
    """The control loop's one Part 21 entry point for one cycle.
    `current_quotes_by_trade_id` maps a pending ticket's `trade_id` to
    `(current_net_bid, current_net_ask, market_data_timestamp)` --
    supplied by the caller, which already knows how to price this
    ticket's specific legs from this cycle's quality-gated chain data;
    this module has no opinion on how a multi-leg net price is derived
    from individual contracts, only on what to do once it has one.
    One ticket's missing quote never blocks evaluating the rest (Part
    7's isolation doctrine, applied here too)."""
    findings: list[TicketMonitorFinding] = []
    repriced: list[FidelityTradeTicket] = []

    for ticket in tickets:
        if ticket.status not in PENDING_TICKET_STATUSES:
            continue
        quote = current_quotes_by_trade_id.get(ticket.trade_id)
        bid, ask, ts = quote if quote is not None else (None, None, None)
        finding = evaluate_pending_ticket(
            ticket, as_of=as_of, current_net_bid=bid, current_net_ask=ask,
            current_market_data_timestamp=ts, max_quote_age=max_quote_age, max_price_drift_pct=max_price_drift_pct,
        )
        findings.append(finding)
        if finding.action != TicketMonitorAction.OK:
            repriced.append(apply_ticket_monitor_finding(ticket, finding, at=as_of))

    return TicketMonitorResult(findings=tuple(findings), repriced_tickets=tuple(repriced))


def record_confirmed_fill(ticket: FidelityTradeTicket, confirmation: ExecutionConfirmation) -> FidelityTradeTicket:
    """The one Part 22 wiring point this module adds: a thin,
    documented call-through to the EXISTING `confirm_fill()` -- kept
    here only so the control loop has one obvious place to call for
    "a human just told me this ticket filled," rather than importing
    `src.brokers.fidelity` directly and risking a future caller
    reaching for `transition()` instead (which `confirm_fill`'s own
    docstring already forbids for a fill). The resulting
    `ConfirmedFidelityPosition` a human later reports goes to
    `src.workflows.reconciliation.reconcile_portfolio`, unchanged --
    this function does not touch `Portfolio` at all."""
    return confirm_fill(ticket, confirmation)
