"""Order Validator (Step 12): the pipeline stage between the
deterministic Risk Engine and `PaperBroker`. This module never
re-litigates a risk decision — `src.risk.engine.evaluate_trade_proposal`
is still the sole authority on whether a trade is approved at all — it
only confirms that an already-APPROVE/RESIZE decision can actually be
turned into a well-formed, broker-appropriate order request, and refuses
if it cannot.

Every failure here raises `OrderValidationError`; the pipeline
orchestrator (`src.orchestration.pipeline`) is the only intended caller
and converts every one of these into `PipelineOutcome.REJECT_ORDER` —
this module itself never talks to a broker or places anything.
"""
from __future__ import annotations

from datetime import date

from src.brokers.base import OrderAction, OrderLeg, OrderType, PlaceOrderRequest
from src.brokers.fidelity import ApprovedOrder, FidelityLegAction, FidelityOrderLeg
from src.data.option_chain import OptionRight
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.reason_codes import RiskDecision


class OrderValidationError(ValueError):
    """Every validation failure this stage catches — never lets a
    malformed or inappropriate order reach `PaperBroker.place_order`."""


_ACTION_MAP: dict[FidelityLegAction, OrderAction] = {
    FidelityLegAction.BUY_TO_OPEN: OrderAction.BUY,
    FidelityLegAction.SELL_TO_OPEN: OrderAction.SELL,
    FidelityLegAction.BUY_TO_CLOSE: OrderAction.BUY,
    FidelityLegAction.SELL_TO_CLOSE: OrderAction.SELL,
}


def build_occ_symbol(ticker: str, expiration: date, right: OptionRight, strike: float) -> str:
    """A plain OCC-style option symbol: TICKER + YYMMDD + C/P + strike
    (thousandths, zero-padded to 8 digits) — matches the format this
    codebase's test fixtures and `PaperBroker` already use to key
    contracts, so an `ApprovedOrder`'s legs (which carry no symbol field
    of their own, only right/strike/expiration) can be looked up in a
    loaded `OptionChain` by symbol like every other leg in this system.
    """
    strike_thousandths = round(strike * 1000)
    return f"{ticker}{expiration.strftime('%y%m%d')}{right.value}{strike_thousandths:08d}"


def validate_and_build_order_request(
    approved_order: ApprovedOrder | None,
    *,
    risk_decision: RiskDecision,
    approved_contracts: int | None,
    broker_capabilities: BrokerCapabilities | None,
    existing_client_order_ids: frozenset[str] = frozenset(),
    client_order_id: str | None = None,
) -> PlaceOrderRequest:
    """The single choke point that turns a Risk-Engine-approved order
    into a `PlaceOrderRequest` `PaperBroker` can accept — or refuses to,
    raising `OrderValidationError` with a specific reason. Every
    parameter is required explicitly (no defaults that could paper over
    a caller forgetting to pass one), matching this platform's "a
    missing stage means REJECT ORDER" rule at the parameter level.

    TS-004 defense-in-depth: `approved_order` is `None` for any decision
    the Risk Engine doesn't build one for (today, that's every non-OPEN
    action, since the Risk Engine itself now rejects those explicitly
    before ever reaching here — see its TS-004 fix). This module must
    never assume `approved_order` is real just because it was told
    APPROVE/RESIZE; the check below turns a missing order into this
    module's own clean `OrderValidationError` rather than the
    `AttributeError` a first unguarded `approved_order.quantity` access
    used to raise — one that the pipeline's Order Validator stage
    couldn't catch (it only catches `OrderValidationError`), crashing
    the entire pipeline call."""
    if approved_order is None:
        raise OrderValidationError(
            "no ApprovedOrder to place an order for — the Risk Engine did not build one for this proposal"
        )
    if risk_decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE):
        raise OrderValidationError(
            f"cannot place an order for a Risk Engine decision of {risk_decision.value!r} — "
            "only APPROVE or RESIZE may reach the Order Validator"
        )
    if approved_contracts is None or approved_contracts <= 0:
        raise OrderValidationError("no positive approved_contracts to place an order for")
    if approved_order.quantity != approved_contracts:
        raise OrderValidationError(
            f"ApprovedOrder.quantity={approved_order.quantity} does not match Risk Engine's "
            f"approved_contracts={approved_contracts} — refusing to place an order for a mismatched size"
        )
    if broker_capabilities is None:
        raise OrderValidationError("no broker capability explicitly configured for this order's target broker")
    if broker_capabilities.execution_mode != "AUTOMATED":
        raise OrderValidationError(
            f"{broker_capabilities.broker_name} is execution_mode={broker_capabilities.execution_mode!r}, not "
            "AUTOMATED — PaperBroker only accepts orders for a broker capability explicitly marked AUTOMATED"
        )
    if not broker_capabilities.options_enabled:
        raise OrderValidationError(f"{broker_capabilities.broker_name}: options are not enabled")

    cid = client_order_id or approved_order.risk_approval_id
    if cid in existing_client_order_ids:
        raise OrderValidationError(f"duplicate order: client_order_id={cid!r} has already been submitted")

    legs = [_build_leg(approved_order, leg) for leg in approved_order.legs]

    return PlaceOrderRequest(
        client_order_id=cid,
        legs=legs,
        order_type=OrderType.LIMIT,
        limit_price=abs(approved_order.limit_price),
    )


def _build_leg(approved_order: ApprovedOrder, leg: FidelityOrderLeg) -> OrderLeg:
    action = _ACTION_MAP.get(leg.action)
    if action is None:  # pragma: no cover - FidelityLegAction is exhaustive today
        raise OrderValidationError(f"no OrderAction mapping for {leg.action!r}")
    symbol = build_occ_symbol(approved_order.ticker, leg.expiration, leg.put_call, leg.strike)
    return OrderLeg(symbol=symbol, right=leg.put_call, strike=leg.strike, expiration=leg.expiration, action=action, quantity=leg.contracts)
