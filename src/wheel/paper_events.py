"""Wires `src.wheel.lifecycle`'s pure state transitions to real
`PaperBroker` orders and settlements (Part 14). Every function here
places or reads actual `PaperBroker` state — the fill price, commission,
and assignment facts a `WheelPosition` gets updated with always come from
here, never invented. Nothing in this module talks to Fidelity or any
live brokerage; `PaperBroker` is the same in-memory simulator every other
strategy in this platform already uses (`src.brokers.paper`).

**Opening a CSP or CC still goes through the unmodified Risk Engine.**
`submit_csp_open`/`submit_cc_open` require an already-approved
`RiskDecisionResult` (from `src.risk.engine.evaluate_trade_proposal`,
called by the caller exactly as it would be for a standalone CSP/CC) —
this module refuses to submit anything the Risk Engine did not
APPROVE/RESIZE. **Closing an existing option early is a pre-existing,
documented gap in this platform's Risk Engine**
(`src.risk.engine._evaluate` rejects every non-OPEN `TradeAction` with
`REJECT_UNSUPPORTED_ACTION` — see that module's TS-004 comment), so a
discretionary buy-to-close order is submitted directly to `PaperBroker`
here, the same way it would have to be for a standalone CSP/CC position
in this codebase today; Step 22.2 does not change that boundary, and
`src.wheel.lifecycle`'s own accounting (Part 11: "never conceal a
realized loss through roll accounting") still records the real fill
price regardless of which path submitted it.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from src.brokers.base import Order, OrderAction, OrderLeg, OrderStatus, OrderType, PlaceOrderRequest
from src.brokers.order_validator import build_occ_symbol, validate_and_build_order_request
from src.brokers.paper import PaperBroker
from src.data.option_chain import OptionRight
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.reason_codes import RiskDecision
from src.wheel import lifecycle
from src.wheel.models import WheelPosition


class WheelOrderNotApprovedError(ValueError):
    """Raised when a caller tries to submit a Wheel CSP/CC open that the
    Risk Engine did not APPROVE or RESIZE — the concrete mechanism
    behind "the Wheel must pass the same portfolio risk engine as every
    other strategy" (Part 3), enforced here as well as at the Risk
    Engine itself (defense in depth, same pattern
    `src.wheel.lifecycle.UncoveredCallError` uses for Part 7)."""


def _new_client_order_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


async def submit_csp_open(
    broker: PaperBroker, wheel: WheelPosition, *, risk_result, proposal_id: str,
    broker_capabilities: BrokerCapabilities, now: datetime,
) -> tuple[WheelPosition, Order]:
    """Submits a Risk-Engine-approved CSP open to `PaperBroker`. If it
    fills immediately (the common case for a liquid, correctly-priced
    limit order in `PaperBroker`'s simulation), the Wheel transitions
    `WHEEL_CANDIDATE -> CSP_OPEN` with the *actual* fill price/commission.
    If it only partially fills or rests unfilled, the Wheel is returned
    unchanged — no cycle is recorded until PaperBroker actually reports a
    fill, so a resting order can never be double-counted by a later
    retry (`retry_fill` below)."""
    if risk_result.decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE) or risk_result.approved_order is None:
        raise WheelOrderNotApprovedError(f"Risk Engine did not approve this CSP open: {risk_result.decision}")

    client_order_id = _new_client_order_id("wheel-csp")
    request = validate_and_build_order_request(
        risk_result.approved_order, risk_decision=risk_result.decision, approved_contracts=risk_result.approved_contracts,
        broker_capabilities=broker_capabilities, client_order_id=client_order_id,
    )
    order = await broker.place_order(request)
    if order.status != OrderStatus.FILLED or order.filled_quantity <= 0:
        return wheel, order

    fills = [f for f in await broker.get_fills() if f.client_order_id == client_order_id]
    commission = sum(f.commission for f in fills)
    leg = risk_result.approved_order.legs[0]
    updated = lifecycle.open_csp(
        wheel, strike=leg.strike, expiration=leg.expiration, contracts=order.filled_quantity,
        premium_per_share=order.avg_fill_price or 0.0, commission=commission,
        proposal_id=proposal_id, position_id=order.broker_order_id, now=now,
    )
    return updated, order


async def submit_cc_open(
    broker: PaperBroker, wheel: WheelPosition, *, risk_result, proposal_id: str,
    broker_capabilities: BrokerCapabilities, below_acquisition_basis: bool, below_economic_basis: bool,
    max_loss_if_called_away: float | None, now: datetime,
) -> tuple[WheelPosition, Order]:
    """Same shape as `submit_csp_open`, for the covered-call leg. Part 7's
    "only against shares actually owned" is enforced twice independently
    here: once by `PaperBroker._required_collateral`/`_has_covering_shares`
    when the order is placed (a naked call attempt is rejected by the
    broker itself), and again by `src.wheel.lifecycle.open_cc`'s own
    `UncoveredCallError` check against this Wheel's own share count."""
    if risk_result.decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE) or risk_result.approved_order is None:
        raise WheelOrderNotApprovedError(f"Risk Engine did not approve this covered call open: {risk_result.decision}")

    client_order_id = _new_client_order_id("wheel-cc")
    request = validate_and_build_order_request(
        risk_result.approved_order, risk_decision=risk_result.decision, approved_contracts=risk_result.approved_contracts,
        broker_capabilities=broker_capabilities, client_order_id=client_order_id,
    )
    order = await broker.place_order(request)
    if order.status != OrderStatus.FILLED or order.filled_quantity <= 0:
        return wheel, order

    fills = [f for f in await broker.get_fills() if f.client_order_id == client_order_id]
    commission = sum(f.commission for f in fills)
    leg = risk_result.approved_order.legs[0]
    updated = lifecycle.open_cc(
        wheel, strike=leg.strike, expiration=leg.expiration, contracts=order.filled_quantity,
        premium_per_share=order.avg_fill_price or 0.0, commission=commission,
        proposal_id=proposal_id, position_id=order.broker_order_id, now=now,
        below_acquisition_basis=below_acquisition_basis, below_economic_basis=below_economic_basis,
        max_loss_if_called_away=max_loss_if_called_away,
    )
    return updated, order


async def retry_fill(broker: PaperBroker, client_order_id: str) -> Order:
    """Thin pass-through to `PaperBroker.attempt_fill`, exposed here so
    a caller managing a resting Wheel order doesn't need to import
    `src.brokers.paper` directly. Recording the consequence on the
    `WheelPosition` once this fills is the caller's job (via
    `src.wheel.lifecycle.open_csp`/`open_cc`, exactly as
    `submit_csp_open`/`submit_cc_open` do internally for an immediate
    fill)."""
    return await broker.attempt_fill(client_order_id)


async def _submit_buy_to_close(
    broker: PaperBroker, *, ticker: str, right: OptionRight, strike: float, expiration: date,
    contracts: int, limit_price: float,
) -> tuple[Order, float]:
    symbol = build_occ_symbol(ticker, expiration, right, strike)
    client_order_id = _new_client_order_id("wheel-btc")
    request = PlaceOrderRequest(
        client_order_id=client_order_id,
        legs=[OrderLeg(symbol=symbol, right=right, strike=strike, expiration=expiration, action=OrderAction.BUY, quantity=contracts)],
        order_type=OrderType.LIMIT, limit_price=limit_price,
    )
    order = await broker.place_order(request)
    fills = [f for f in await broker.get_fills() if f.client_order_id == client_order_id]
    commission = sum(f.commission for f in fills)
    return order, commission


async def buy_to_close_csp(broker: PaperBroker, wheel: WheelPosition, *, limit_price: float, now: datetime) -> tuple[WheelPosition, Order]:
    cycle = wheel.open_csp_cycle
    if cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CSP cycle to buy to close")
    order, commission = await _submit_buy_to_close(
        broker, ticker=wheel.ticker, right=OptionRight.PUT, strike=cycle.strike, expiration=cycle.expiration,
        contracts=cycle.contracts, limit_price=limit_price,
    )
    if order.status != OrderStatus.FILLED:
        return wheel, order
    updated = lifecycle.csp_bought_to_close(wheel, buyback_price_per_share=order.avg_fill_price or 0.0, commission=commission, now=now)
    return updated, order


async def buy_to_close_cc(broker: PaperBroker, wheel: WheelPosition, *, limit_price: float, now: datetime) -> tuple[WheelPosition, Order]:
    cycle = wheel.open_cc_cycle
    if cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CC cycle to buy to close")
    order, commission = await _submit_buy_to_close(
        broker, ticker=wheel.ticker, right=OptionRight.CALL, strike=cycle.strike, expiration=cycle.expiration,
        contracts=cycle.contracts, limit_price=limit_price,
    )
    if order.status != OrderStatus.FILLED:
        return wheel, order
    updated = lifecycle.cc_bought_to_close(wheel, buyback_price_per_share=order.avg_fill_price or 0.0, commission=commission, now=now)
    return updated, order


def settle_csp_expiration(broker: PaperBroker, wheel: WheelPosition, *, settlement_price: float, now: datetime) -> WheelPosition:
    """Calls `PaperBroker.settle_expiration` (synchronous, no network)
    and interprets its `ExpirationSettlement` result for this Wheel's
    open CSP cycle: assigned -> `lifecycle.csp_assigned`, expired OTM ->
    `lifecycle.csp_expires_worthless`. Never fabricates the outcome --
    if PaperBroker reports no matching settlement, this raises rather
    than guessing."""
    cycle = wheel.open_csp_cycle
    if cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CSP cycle to settle")
    settlements = broker.settle_expiration(wheel.ticker, cycle.expiration, settlement_price)
    match = next((s for s in settlements if s.right == OptionRight.PUT and s.strike == cycle.strike), None)
    if match is None:
        raise ValueError(f"PaperBroker reported no settlement matching Wheel {wheel.wheel_id}'s open CSP cycle")
    if match.assigned_or_exercised:
        return lifecycle.csp_assigned(wheel, now=now)
    return lifecycle.csp_expires_worthless(wheel, now=now)


def settle_cc_expiration(broker: PaperBroker, wheel: WheelPosition, *, settlement_price: float, now: datetime) -> WheelPosition:
    cycle = wheel.open_cc_cycle
    if cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CC cycle to settle")
    settlements = broker.settle_expiration(wheel.ticker, cycle.expiration, settlement_price)
    match = next((s for s in settlements if s.right == OptionRight.CALL and s.strike == cycle.strike), None)
    if match is None:
        raise ValueError(f"PaperBroker reported no settlement matching Wheel {wheel.wheel_id}'s open CC cycle")
    if match.assigned_or_exercised:
        return lifecycle.shares_called_away(wheel, now=now)
    return lifecycle.cc_expires_worthless(wheel, now=now)
