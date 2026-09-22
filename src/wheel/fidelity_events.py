"""Wheel <-> Fidelity manual-execution wiring (Part 20).

**Nothing in this module is new Fidelity capability.** Every Wheel CSP/CC
*open* leg is a completely ordinary `TradeProposal` with
`strategy=StrategyType.CASH_SECURED_PUT`/`COVERED_CALL` submitted to the
exact same unmodified `src.risk.engine.evaluate_trade_proposal`, which
already builds a `FidelityTradeTicket` automatically
(`FidelityManualProvider.generate_trade_ticket`) whenever a MANUAL-broker
capability is supplied — this module adds no ticket-generation logic of
its own, only the glue that ties a resulting ticket back to a `wheel_id`
and, once a human reports `ORDER_ENTERED`/`FILLED` via the existing
`ExecutionConfirmation` mechanism, forwards those fill facts into
`src.wheel.lifecycle` exactly the way `src.wheel.paper_events` does for
the `PaperBroker` path. `src/brokers/fidelity.py` itself (the trusted
kernel CLAUDE.md names explicitly) is never modified or subclassed here.

**Discretionary buy-to-close tickets are a known, pre-existing platform
gap, not newly introduced or newly solved by this module.**
`ApprovedOrder`/`FidelityTradeTicket` are structurally shaped for an
*opening* trade only (`max_profit`/`max_loss`/`breakeven` are all
required positive fields with no natural meaning for a closing order,
and `src.risk.engine._evaluate` already rejects every non-OPEN
`TradeAction` outright — the same TS-004 gap `src.risk.engine`'s own
module docstring documents). Retrofitting fabricated "max profit" onto a
close order to force it through that schema would mean inventing a
number with no real meaning, which CLAUDE.md forbids outright. A human
closing a Wheel's CSP/CC early therefore reads the position's strike/
expiration/contracts off the dashboard (already fully visible there,
Part 19) and manually enters "BUY TO CLOSE" in Trader+ themselves —
exactly how they would already have to for any standalone CSP/CC
position in this platform today, Wheel or not — and reports the fill
back the same way, via `record_wheel_buy_to_close_fill` below.

**Assignment is a reconciliation event, never a fabricated order**
(Part 20's explicit requirement): `record_wheel_csp_assignment`/
`record_wheel_cc_assignment` below never construct or touch a
`FidelityTradeTicket` — they only forward a human's plain confirmation
that an assignment happened into `src.wheel.lifecycle`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.brokers.fidelity import ApprovedOrder, FidelityManualProvider, FidelityTradeTicket
from src.risk.reason_codes import RiskDecision
from src.wheel import lifecycle
from src.wheel.models import WheelPosition


class WheelTicketNotApprovedError(ValueError):
    """Mirrors `src.wheel.paper_events.WheelOrderNotApprovedError` for
    the Fidelity manual path: a Wheel never gets a ticket the Risk
    Engine did not APPROVE/RESIZE."""


@dataclass(frozen=True)
class WheelFidelityTicket:
    """A `FidelityTradeTicket`, unmodified, paired with the `wheel_id`
    it belongs to -- kept as an external mapping (this dataclass) rather
    than a new field bolted onto `FidelityTradeTicket` itself, since that
    schema is part of the trusted kernel CLAUDE.md says never to weaken
    or casually extend."""

    wheel_id: str
    ticket: FidelityTradeTicket


def build_wheel_csp_open_ticket(wheel: WheelPosition, *, risk_result, now: datetime) -> WheelFidelityTicket:
    if risk_result.decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE) or risk_result.fidelity_ticket is None:
        raise WheelTicketNotApprovedError(
            f"Risk Engine did not produce a Fidelity ticket for this Wheel's CSP open: {risk_result.decision}"
        )
    return WheelFidelityTicket(wheel_id=wheel.wheel_id, ticket=risk_result.fidelity_ticket)


def build_wheel_cc_open_ticket(wheel: WheelPosition, *, risk_result, now: datetime) -> WheelFidelityTicket:
    if risk_result.decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE) or risk_result.fidelity_ticket is None:
        raise WheelTicketNotApprovedError(
            f"Risk Engine did not produce a Fidelity ticket for this Wheel's covered call open: {risk_result.decision}"
        )
    return WheelFidelityTicket(wheel_id=wheel.wheel_id, ticket=risk_result.fidelity_ticket)


def record_wheel_csp_open_fill(wheel: WheelPosition, *, ticket: FidelityTradeTicket, commission: float, now: datetime) -> WheelPosition:
    """Once a human has confirmed `FILLED` on `ticket` (via this
    platform's existing `FidelityTradeTicket.confirm_fill`, outside this
    module's scope), the real fill facts on the ticket's own
    `execution_confirmation` drive the Wheel's own state -- never a
    second, independently-guessed premium. `commission` is a separate,
    explicit parameter because `ExecutionConfirmation` (a real Fidelity
    fill has no modeled commission field of its own — a human reports
    whatever their actual account was charged, same as every other
    Fidelity-executed position in this platform)."""
    confirmation = ticket.execution_confirmation
    if confirmation is None:
        raise ValueError("ticket has no execution_confirmation -- nothing to record yet")
    leg = ticket.legs[0]
    return lifecycle.open_csp(
        wheel, strike=leg.strike, expiration=leg.expiration, contracts=confirmation.filled_quantity,
        premium_per_share=confirmation.fill_price, commission=commission,
        proposal_id=None, position_id=ticket.risk_approval_id, now=now,
    )


def record_wheel_cc_open_fill(wheel: WheelPosition, *, ticket: FidelityTradeTicket, commission: float, now: datetime, below_acquisition_basis: bool, below_economic_basis: bool, max_loss_if_called_away: float | None) -> WheelPosition:
    confirmation = ticket.execution_confirmation
    if confirmation is None:
        raise ValueError("ticket has no execution_confirmation -- nothing to record yet")
    leg = ticket.legs[0]
    return lifecycle.open_cc(
        wheel, strike=leg.strike, expiration=leg.expiration, contracts=confirmation.filled_quantity,
        premium_per_share=confirmation.fill_price, commission=commission,
        proposal_id=None, position_id=ticket.risk_approval_id, now=now,
        below_acquisition_basis=below_acquisition_basis, below_economic_basis=below_economic_basis,
        max_loss_if_called_away=max_loss_if_called_away,
    )


def record_wheel_buy_to_close_fill(wheel: WheelPosition, *, leg: str, fill_price_per_share: float, commission: float, now: datetime) -> WheelPosition:
    """`leg` is `"csp"` or `"cc"`. See this module's docstring for why
    this is a plain human-reported fill rather than a generated
    ticket -- the same manual-entry-then-report pattern the rest of this
    platform's Fidelity workflow already uses, applied here since no
    CLOSE-order ticket path exists anywhere in this codebase yet."""
    if leg == "csp":
        return lifecycle.csp_bought_to_close(wheel, buyback_price_per_share=fill_price_per_share, commission=commission, now=now)
    if leg == "cc":
        return lifecycle.cc_bought_to_close(wheel, buyback_price_per_share=fill_price_per_share, commission=commission, now=now)
    raise ValueError(f"leg must be 'csp' or 'cc', got {leg!r}")


def record_wheel_csp_assignment(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """A human reports (outside Fidelity's own ticket lifecycle, since
    assignment is not itself an order Fidelity ever showed a limit price
    for) that this Wheel's short put was assigned. Reconciliation event,
    not a fabricated order -- Part 20's explicit requirement."""
    return lifecycle.csp_assigned(wheel, now=now)


def record_wheel_cc_assignment(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """Same as `record_wheel_csp_assignment`, for a covered call finishing
    ITM and the shares being called away."""
    return lifecycle.shares_called_away(wheel, now=now)
