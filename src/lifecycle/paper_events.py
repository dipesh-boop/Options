"""Part 20: wires lifecycle-driven actions (close, partial close,
expiration, assignment, exercise, call-away) to real `PaperBroker`
orders and settlements.

This module never computes a dollar figure — the fill price,
commission, and assignment facts always come from `PaperBroker` itself
(mirroring `src.wheel.paper_events`'s structure, for every non-Wheel
strategy in this platform's library; a Wheel position keeps using
`src.wheel.paper_events` directly, this module never duplicates it).

**Opening a genuinely new position — including a roll's OPEN half — is
NOT this module's job.** A roll's new leg is a separate `TradeProposal`
that must independently clear the full existing approval pipeline
(`src.orchestration.pipeline.run_order_pipeline`: Quant -> Devil's
Advocate -> Portfolio Manager -> Risk -> PaperBroker/Fidelity), exactly
like any other proposal — rolling and lifecycle management grant no
shortcut through it. This module only ever submits a *close* leg
discretionarily, the same pre-existing, documented boundary
`src.wheel.paper_events` describes: `src.risk.engine._evaluate` only
supports the OPEN `TradeAction` today (see that module's TS-004
comment), so a lifecycle-driven close/roll-close/adjustment-close is
submitted directly to `PaperBroker`, exactly as a Wheel buy-to-close
already is.

**PaperBroker never uses theoretical mid as guaranteed execution**
(Part 20) — the limit price a caller supplies here must be a real,
already-quoted bid/ask-aware price; `PaperBroker.compute_fill`'s own
bid/ask/slippage model decides the actual fill, this module never
invents or assumes one.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from src.brokers.base import Order, OrderAction, OrderLeg, OrderType, PlaceOrderRequest
from src.brokers.order_validator import build_occ_symbol
from src.brokers.paper import ExpirationSettlement, PaperBroker
from src.data.option_chain import OptionRight


@dataclass(frozen=True)
class ClosingLeg:
    """One leg of an already-open position that a lifecycle action
    (close/partial close/roll-close/adjustment-close) needs to close.
    `original_action` is the action that OPENED this leg (`BUY` for a
    long leg, `SELL` for a short one) — the closing order's own action
    is always the opposite, computed here, never supplied by the
    caller, so a caller can never accidentally submit a close on the
    wrong side."""

    ticker: str
    right: OptionRight
    strike: float
    expiration: date
    original_action: OrderAction
    quantity: int  # <= the originally open quantity; less than that is a genuine Part 20 partial close


def _new_client_order_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _closing_order_leg(leg: ClosingLeg) -> OrderLeg:
    symbol = build_occ_symbol(leg.ticker, leg.expiration, leg.right, leg.strike)
    close_action = OrderAction.BUY if leg.original_action == OrderAction.SELL else OrderAction.SELL
    return OrderLeg(
        symbol=symbol, right=leg.right, strike=leg.strike, expiration=leg.expiration, action=close_action, quantity=leg.quantity
    )


async def close_position(
    broker: PaperBroker, *, legs: list[ClosingLeg], limit_price: float, client_order_id: str | None = None
) -> tuple[Order, float]:
    """Submits a close order for every leg in `legs` as one combo
    order (1-4 legs, matching `PlaceOrderRequest`'s own limit). Returns
    the resulting `Order` and the total commission `PaperBroker`
    actually charged on its fills — `0.0` if it did not fill.
    `legs` whose `quantity` is less than the position's full open
    quantity produces a genuine PARTIALLY_CLOSED-shaped close (Part 20's
    partial-close support) — `PaperBroker.place_order` has no separate
    "close everything" concept, it only ever fills the exact quantity
    requested, so a partial close is simply a close order for less than
    the full open quantity."""
    order_legs = [_closing_order_leg(leg) for leg in legs]
    coid = client_order_id or _new_client_order_id("lifecycle-close")
    request = PlaceOrderRequest(client_order_id=coid, legs=order_legs, order_type=OrderType.LIMIT, limit_price=limit_price)
    order = await broker.place_order(request)
    fills = [f for f in await broker.get_fills() if f.client_order_id == coid]
    commission = sum(f.commission for f in fills)
    return order, commission


def settle_lifecycle_expiration(
    broker: PaperBroker, *, underlying_symbol: str, expiration: date, settlement_price: float
) -> list[ExpirationSettlement]:
    """Thin, non-Wheel-specific pass-through to
    `PaperBroker.settle_expiration`, which is already fully generic
    across every leg on `underlying_symbol` expiring on `expiration` —
    this module adds no new settlement logic of its own, only the
    lifecycle-facing entry point Part 20 asks for. `ExpirationSettlement
    .assigned_or_exercised`/`.share_impact` together already express
    assignment, exercise, and call-away (a short call's
    `assigned_or_exercised=True` with a negative `share_impact` IS a
    call-away), exactly as `src.wheel.paper_events.settle_csp_expiration`/
    `settle_cc_expiration` already interpret them for the Wheel case."""
    return broker.settle_expiration(underlying_symbol, expiration, settlement_price)


def matching_settlement(
    settlements: list[ExpirationSettlement], *, right: OptionRight, strike: float
) -> ExpirationSettlement | None:
    """Finds the one `ExpirationSettlement` (if any) matching a
    specific leg's right/strike out of a whole-underlying settlement
    batch. Returns `None` rather than guessing when `PaperBroker`
    reported no matching settlement — the caller must raise on that
    explicitly (Part 19's fail-safe posture applies here too: a missing
    settlement is never silently treated as "expired worthless")."""
    return next((s for s in settlements if s.right == right and s.strike == strike), None)
