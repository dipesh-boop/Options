"""Wheel lifecycle orchestration: one function per edge in
`src.wheel.state`'s state machine. Every function here is pure — it
takes a `WheelPosition` and returns a new one via `.model_copy(update=...)`,
never mutating its argument, and never calling the network, an LLM, or a
broker directly. The actual CSP/CC order placement (PaperBroker fills,
Fidelity tickets) happens in the caller (`src.wheel.paper_events` for the
PaperBroker path); this module only records the *consequence* of that
order on the Wheel's own state and accounting once the caller already
knows the fill/settlement facts.

**No function here ever re-opens a CSP/CC automatically.** Every
"terminal for this cycle" function (`csp_expires_worthless`,
`csp_bought_to_close`, `cc_expires_worthless`, `cc_bought_to_close`)
returns a `WheelPosition` in a resting state (`CSP_EXPIRED`/`CSP_CLOSED`
are Wheel-terminal; `CC_ELIGIBLE` after a CC cycle closes) — starting a
new cycle is always a separate, explicit call the caller makes only
after that candidate has again passed eligibility, Quant, Risk, and (for
a brand new Wheel) strategy competition against CASH, per Part 10/16.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from src.wheel.accounting import (
    assigned as _acc_assigned,
    called_away as _acc_called_away,
    cc_premium_received as _acc_cc_premium_received,
    cc_closed_unassigned as _acc_cc_closed_unassigned,
    csp_opened as _acc_csp_opened,
    csp_premium_received as _acc_csp_premium_received,
    csp_released_unassigned as _acc_csp_released_unassigned,
)
from src.wheel.models import (
    CcCloseReason,
    CcCycle,
    CspCloseReason,
    CspCycle,
    WheelEvent,
    WheelEventType,
    WheelPosition,
    WheelStateTransitionRecord,
)
from src.wheel.state import WheelState, is_terminal, transition

_CONTRACT_MULTIPLIER = 100


class UncoveredCallError(ValueError):
    """Defense in depth (Part 7: "Never allow the Wheel to create an
    uncovered short call"). `PaperBroker`/`src.risk.engine` already
    refuse an uncovered call independently -- this raises even earlier,
    from the Wheel's own ledger, so a bug anywhere upstream of those two
    still cannot construct a `CcCycle` this module believes is covered
    when it isn't."""


def _require_tz(v: datetime, name: str) -> None:
    if v.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _apply_transition(wheel: WheelPosition, target: WheelState, *, reason: str, now: datetime, **extra_updates) -> WheelPosition:
    _require_tz(now, "now")
    new_state = transition(wheel.state, target)
    record = WheelStateTransitionRecord(wheel_id=wheel.wheel_id, from_state=wheel.state, to_state=new_state, occurred_at=now, reason=reason)
    updates = dict(extra_updates)
    updates["state"] = new_state
    updates["state_history"] = wheel.state_history + (record,)
    if is_terminal(new_state) and wheel.completed_at is None:
        updates["completed_at"] = now
    return wheel.model_copy(update=updates)


def _append_event(wheel: WheelPosition, event_type: WheelEventType, *, detail: str, now: datetime, related_id: str | None = None, cash_impact: float = 0.0, share_impact: int = 0) -> WheelPosition:
    event = WheelEvent(
        event_id=_new_id("evt"), wheel_id=wheel.wheel_id, event_type=event_type, occurred_at=now,
        detail=detail, related_id=related_id, cash_impact=cash_impact, share_impact=share_impact,
    )
    return wheel.model_copy(update={"events": wheel.events + (event,)})


# ------------------------------------------------------------- candidate


def open_wheel_candidate(*, wheel_id: str, ticker: str, now: datetime) -> WheelPosition:
    _require_tz(now, "now")
    return WheelPosition(wheel_id=wheel_id, ticker=ticker, state=WheelState.WHEEL_CANDIDATE, started_at=now)


def reject_candidate(wheel: WheelPosition, *, reason: str, now: datetime) -> WheelPosition:
    """Part 2: `WHEEL_REJECTED` -- eligibility or the deterministic Risk
    Engine refused this candidate before any order was ever placed. Only
    reachable from `WHEEL_CANDIDATE`: once a real order exists, unwinding
    it is `exit_wheel`/`halt_wheel`, never a rejection."""
    updated = _apply_transition(wheel, WheelState.WHEEL_REJECTED, reason=reason, now=now, rejection_reason=reason)
    return _append_event(updated, WheelEventType.WHEEL_REJECTED, detail=reason, now=now)


# ------------------------------------------------------------------ CSP


def open_csp(
    wheel: WheelPosition, *, strike: float, expiration: date, contracts: int, premium_per_share: float,
    commission: float, proposal_id: str | None, position_id: str | None, now: datetime,
) -> WheelPosition:
    """Part 3: opens the Wheel's entry CSP. `contracts`/`strike`/
    `premium_per_share`/`commission` must be the actual fill facts (from
    `src.wheel.paper_events`/Fidelity confirmation) -- this function
    performs no pricing of its own."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    cycle = CspCycle(
        cycle_id=_new_id("csp"), wheel_id=wheel.wheel_id, strike=strike, expiration=expiration, contracts=contracts,
        premium_received_per_share=premium_per_share, commissions_paid=commission, proposal_id=proposal_id,
        position_id=position_id, opened_at=now,
    )
    new_accounting = _acc_csp_opened(wheel.accounting, strike=strike, contracts=contracts)
    new_accounting = _acc_csp_premium_received(new_accounting, premium_per_share=premium_per_share, contracts=contracts, commission=commission)
    updated = _apply_transition(
        wheel, WheelState.CSP_OPEN, reason=f"opened CSP {cycle.cycle_id} at strike {strike}",
        now=now, csp_cycles=wheel.csp_cycles + (cycle,), accounting=new_accounting,
    )
    return _append_event(
        updated, WheelEventType.CSP_OPENED, now=now, related_id=cycle.cycle_id,
        detail=f"sold {contracts} put(s) at strike ${strike:.2f} exp {expiration.isoformat()} for ${premium_per_share:.2f}/share",
        cash_impact=premium_per_share * _CONTRACT_MULTIPLIER * contracts - commission,
    )


def _close_csp_cycle(wheel: WheelPosition, *, close_reason: CspCloseReason, close_price_per_share: float, extra_commission: float, now: datetime) -> tuple[WheelPosition, CspCycle]:
    open_cycle = wheel.open_csp_cycle
    if open_cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CSP cycle to close")
    total_commissions = open_cycle.commissions_paid + extra_commission
    realized = (open_cycle.premium_received_per_share - close_price_per_share) * _CONTRACT_MULTIPLIER * open_cycle.contracts - total_commissions
    closed_cycle = open_cycle.model_copy(
        update={"closed_at": now, "close_reason": close_reason, "close_price_per_share": close_price_per_share, "realized_pnl": round(realized, 2), "commissions_paid": round(total_commissions, 2)}
    )
    new_cycles = tuple(closed_cycle if c.cycle_id == open_cycle.cycle_id else c for c in wheel.csp_cycles)
    return wheel.model_copy(update={"csp_cycles": new_cycles}), closed_cycle


def csp_expires_worthless(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """Part 10: the put expires OTM. Premium is fully recognized, the
    reserved cash is released, and this wheel_id is done -- Part 10:
    "Do NOT automatically sell another put. The next CSP must again
    pass" the full pipeline under a fresh candidate/wheel_id."""
    wheel_with_closed_cycle, cycle = _close_csp_cycle(wheel, close_reason=CspCloseReason.EXPIRED_WORTHLESS, close_price_per_share=0.0, extra_commission=0.0, now=now)
    new_accounting = _acc_csp_released_unassigned(wheel_with_closed_cycle.accounting, strike=cycle.strike, contracts=cycle.contracts, realized_pnl=cycle.realized_pnl or 0.0)
    updated = _apply_transition(
        wheel_with_closed_cycle, WheelState.CSP_EXPIRED, reason=f"CSP {cycle.cycle_id} expired worthless", now=now, accounting=new_accounting,
    )
    return _append_event(updated, WheelEventType.CSP_EXPIRED, now=now, related_id=cycle.cycle_id, detail=f"CSP expired worthless; realized P&L ${cycle.realized_pnl:.2f}")


def csp_bought_to_close(wheel: WheelPosition, *, buyback_price_per_share: float, commission: float, now: datetime) -> WheelPosition:
    """Part 3/10: the put was bought back before expiration (a
    discretionary close, e.g. hitting the profit target). Never a
    disguised roll -- Part 11 requires a roll be modeled as a real close
    plus a fully independent new proposal, never concealed accounting."""
    wheel_with_closed_cycle, cycle = _close_csp_cycle(wheel, close_reason=CspCloseReason.BOUGHT_TO_CLOSE, close_price_per_share=buyback_price_per_share, extra_commission=commission, now=now)
    new_accounting = _acc_csp_released_unassigned(wheel_with_closed_cycle.accounting, strike=cycle.strike, contracts=cycle.contracts, realized_pnl=cycle.realized_pnl or 0.0)
    updated = _apply_transition(
        wheel_with_closed_cycle, WheelState.CSP_CLOSED, reason=f"CSP {cycle.cycle_id} bought to close at ${buyback_price_per_share:.2f}", now=now, accounting=new_accounting,
    )
    return _append_event(updated, WheelEventType.CSP_BOUGHT_BACK, now=now, related_id=cycle.cycle_id, detail=f"bought to close at ${buyback_price_per_share:.2f}/share; realized P&L ${cycle.realized_pnl:.2f}")


def csp_assigned(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """Part 6: assignment. The option cycle's own realized P&L is just
    the premium collected (an assignment is not a losing option trade in
    itself -- the resulting stock position's P&L is tracked completely
    separately, never blended into this figure); the stock leg is
    created at cost = strike per Part 6's "initial stock acquisition
    price: put strike"."""
    open_cycle = wheel.open_csp_cycle
    if open_cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CSP cycle to assign")
    realized = open_cycle.premium_received_per_share * _CONTRACT_MULTIPLIER * open_cycle.contracts - open_cycle.commissions_paid
    closed_cycle = open_cycle.model_copy(update={"closed_at": now, "close_reason": CspCloseReason.ASSIGNED, "realized_pnl": round(realized, 2)})
    new_cycles = tuple(closed_cycle if c.cycle_id == open_cycle.cycle_id else c for c in wheel.csp_cycles)
    new_accounting = _acc_assigned(wheel.accounting, strike=closed_cycle.strike, contracts=closed_cycle.contracts, assigned_at=now)

    updated = _apply_transition(
        wheel.model_copy(update={"csp_cycles": new_cycles}), WheelState.ASSIGNED_SHARES,
        reason=f"CSP {closed_cycle.cycle_id} assigned at strike {closed_cycle.strike}", now=now, accounting=new_accounting,
    )
    updated = _append_event(
        updated, WheelEventType.CSP_ASSIGNED, now=now, related_id=closed_cycle.cycle_id,
        detail=f"assigned {closed_cycle.contracts} contract(s) at strike ${closed_cycle.strike:.2f}",
    )
    shares = _CONTRACT_MULTIPLIER * closed_cycle.contracts
    return _append_event(
        updated, WheelEventType.STOCK_POSITION_CREATED, now=now, related_id=closed_cycle.cycle_id,
        detail=f"{shares} shares of {wheel.ticker} created at cost basis ${closed_cycle.strike:.2f}/share",
        share_impact=shares, cash_impact=-closed_cycle.strike * shares,
    )


# ------------------------------------------------------------- covered call


def mark_cc_eligible(wheel: WheelPosition, *, now: datetime, reason: str = "shares available to consider a covered call") -> WheelPosition:
    """Reachable from `ASSIGNED_SHARES`, `CC_EXPIRED`, or `CC_CLOSED` --
    `src.wheel.state.VALID_TRANSITIONS` is the single source of truth
    for which of those apply; this function does not special-case them."""
    return _apply_transition(wheel, WheelState.CC_ELIGIBLE, reason=reason, now=now)


def no_cc_trade(wheel: WheelPosition, *, reason: str, now: datetime) -> WheelPosition:
    """Part 7: "Holding the shares without selling a call must be
    valid." This is deliberately NOT a state transition (the Wheel stays
    at `CC_ELIGIBLE`) -- only an auditable note explaining why no call
    was sold this review cycle, so a 90-day validation report can show
    *why* a Wheel sat uncalled rather than silently going quiet."""
    if wheel.state != WheelState.CC_ELIGIBLE:
        raise ValueError(f"no_cc_trade only applies to a Wheel at CC_ELIGIBLE, not {wheel.state.value}")
    return _append_event(wheel, WheelEventType.NO_CC_TRADE, now=now, detail=reason)


def open_cc(
    wheel: WheelPosition, *, strike: float, expiration: date, contracts: int, premium_per_share: float,
    commission: float, proposal_id: str | None, position_id: str | None, now: datetime,
    below_acquisition_basis: bool, below_economic_basis: bool, max_loss_if_called_away: float | None,
) -> WheelPosition:
    """Part 7: covered calls may ONLY be sold against shares actually
    owned. `contracts` here is call contracts, each covering 100 shares
    -- `UncoveredCallError` is raised (never silently capped) if the
    Wheel's own ledger doesn't show enough shares, mirroring
    `PaperBroker`'s and the Risk Engine's own independent enforcement of
    the same rule."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    required_shares = _CONTRACT_MULTIPLIER * contracts
    if wheel.accounting.shares_owned < required_shares:
        raise UncoveredCallError(
            f"Wheel {wheel.wheel_id} holds {wheel.accounting.shares_owned} shares, fewer than the "
            f"{required_shares} required to cover {contracts} call contract(s)"
        )
    cycle = CcCycle(
        cycle_id=_new_id("cc"), wheel_id=wheel.wheel_id, strike=strike, expiration=expiration, contracts=contracts,
        premium_received_per_share=premium_per_share, commissions_paid=commission, proposal_id=proposal_id,
        position_id=position_id, opened_at=now, below_acquisition_basis=below_acquisition_basis,
        below_economic_basis=below_economic_basis, max_loss_if_called_away=max_loss_if_called_away,
    )
    new_accounting = _acc_cc_premium_received(wheel.accounting, premium_per_share=premium_per_share, contracts=contracts, commission=commission)
    updated = _apply_transition(
        wheel, WheelState.CC_OPEN, reason=f"opened CC {cycle.cycle_id} at strike {strike}",
        now=now, cc_cycles=wheel.cc_cycles + (cycle,), accounting=new_accounting,
    )
    basis_note = ""
    if below_acquisition_basis:
        basis_note = " [BELOW_ACQUISITION_BASIS]" + (" [BELOW_ECONOMIC_BASIS]" if below_economic_basis else "")
    return _append_event(
        updated, WheelEventType.CC_OPENED, now=now, related_id=cycle.cycle_id,
        detail=f"sold {contracts} call(s) at strike ${strike:.2f} exp {expiration.isoformat()} for ${premium_per_share:.2f}/share{basis_note}",
        cash_impact=premium_per_share * _CONTRACT_MULTIPLIER * contracts - commission,
    )


def _close_cc_cycle(wheel: WheelPosition, *, close_reason: CcCloseReason, close_price_per_share: float, extra_commission: float, now: datetime) -> tuple[WheelPosition, CcCycle]:
    open_cycle = wheel.open_cc_cycle
    if open_cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CC cycle to close")
    total_commissions = open_cycle.commissions_paid + extra_commission
    realized = (open_cycle.premium_received_per_share - close_price_per_share) * _CONTRACT_MULTIPLIER * open_cycle.contracts - total_commissions
    closed_cycle = open_cycle.model_copy(
        update={"closed_at": now, "close_reason": close_reason, "close_price_per_share": close_price_per_share, "realized_pnl": round(realized, 2), "commissions_paid": round(total_commissions, 2)}
    )
    new_cycles = tuple(closed_cycle if c.cycle_id == open_cycle.cycle_id else c for c in wheel.cc_cycles)
    return wheel.model_copy(update={"cc_cycles": new_cycles}), closed_cycle


def cc_expires_worthless(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    wheel_with_closed_cycle, cycle = _close_cc_cycle(wheel, close_reason=CcCloseReason.EXPIRED_WORTHLESS, close_price_per_share=0.0, extra_commission=0.0, now=now)
    new_accounting = _acc_cc_closed_unassigned(wheel_with_closed_cycle.accounting, realized_pnl=cycle.realized_pnl or 0.0)
    updated = _apply_transition(wheel_with_closed_cycle, WheelState.CC_EXPIRED, reason=f"CC {cycle.cycle_id} expired worthless", now=now, accounting=new_accounting)
    return _append_event(updated, WheelEventType.CC_EXPIRED, now=now, related_id=cycle.cycle_id, detail=f"CC expired worthless; realized P&L ${cycle.realized_pnl:.2f}")


def cc_bought_to_close(wheel: WheelPosition, *, buyback_price_per_share: float, commission: float, now: datetime) -> WheelPosition:
    wheel_with_closed_cycle, cycle = _close_cc_cycle(wheel, close_reason=CcCloseReason.BOUGHT_TO_CLOSE, close_price_per_share=buyback_price_per_share, extra_commission=commission, now=now)
    new_accounting = _acc_cc_closed_unassigned(wheel_with_closed_cycle.accounting, realized_pnl=cycle.realized_pnl or 0.0)
    updated = _apply_transition(wheel_with_closed_cycle, WheelState.CC_CLOSED, reason=f"CC {cycle.cycle_id} bought to close at ${buyback_price_per_share:.2f}", now=now, accounting=new_accounting)
    return _append_event(updated, WheelEventType.CC_BOUGHT_BACK, now=now, related_id=cycle.cycle_id, detail=f"bought to close at ${buyback_price_per_share:.2f}/share; realized P&L ${cycle.realized_pnl:.2f}")


def shares_called_away(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """Part 9: the covered call finished ITM and the shares were
    assigned away. Closes the CC cycle (its own option P&L is just the
    premium collected, exactly like `csp_assigned`) and realizes the
    stock leg's P&L against the tax-style acquisition basis."""
    open_cycle = wheel.open_cc_cycle
    if open_cycle is None:
        raise ValueError(f"Wheel {wheel.wheel_id} has no open CC cycle to call away")
    realized = open_cycle.premium_received_per_share * _CONTRACT_MULTIPLIER * open_cycle.contracts - open_cycle.commissions_paid
    closed_cycle = open_cycle.model_copy(update={"closed_at": now, "close_reason": CcCloseReason.ASSIGNED_CALLED_AWAY, "realized_pnl": round(realized, 2)})
    new_cycles = tuple(closed_cycle if c.cycle_id == open_cycle.cycle_id else c for c in wheel.cc_cycles)
    new_accounting = _acc_called_away(wheel.accounting, strike=closed_cycle.strike, contracts=closed_cycle.contracts)

    updated = _apply_transition(
        wheel.model_copy(update={"cc_cycles": new_cycles}), WheelState.SHARES_CALLED_AWAY,
        reason=f"CC {closed_cycle.cycle_id} assigned; shares called away at strike {closed_cycle.strike}", now=now, accounting=new_accounting,
    )
    shares = _CONTRACT_MULTIPLIER * closed_cycle.contracts
    return _append_event(
        updated, WheelEventType.SHARES_CALLED_AWAY, now=now, related_id=closed_cycle.cycle_id,
        detail=f"{shares} shares of {wheel.ticker} called away at strike ${closed_cycle.strike:.2f}",
        share_impact=-shares, cash_impact=closed_cycle.strike * shares,
    )


def complete_wheel(wheel: WheelPosition, *, now: datetime) -> WheelPosition:
    """Part 9: `WHEEL_COMPLETE` is terminal -- Part 9: "Do NOT
    automatically initiate another CSP. A new Wheel must compete again
    against all strategies and CASH", enforced structurally by
    `WHEEL_COMPLETE` having no outgoing edge in `src.wheel.state`."""
    updated = _apply_transition(wheel, WheelState.WHEEL_COMPLETE, reason="Wheel complete: full cycle finished with shares called away", now=now)
    return _append_event(updated, WheelEventType.WHEEL_CLOSED, now=now, detail="Wheel complete")


# --------------------------------------------------------------- escape hatches


def halt_wheel(wheel: WheelPosition, *, reason: str, now: datetime) -> WheelPosition:
    """Reachable from any non-terminal state -- e.g. a kill-switch/
    drawdown halt (`src.risk.kill_switch`) applies to a Wheel's account
    exactly as it applies to every other open position."""
    updated = _apply_transition(wheel, WheelState.WHEEL_HALTED, reason=reason, now=now, halt_reason=reason)
    return _append_event(updated, WheelEventType.WHEEL_HALTED, now=now, detail=reason)


def exit_wheel(wheel: WheelPosition, *, reason: str, now: datetime) -> WheelPosition:
    """A human (or a future automated close-out, still gated by the Risk
    Engine on the actual closing order) decides to unwind this Wheel
    outside its normal cycle -- e.g. buying back an open option and/or
    selling the shares outright. This function only records the
    resulting state; the actual closing trades still go through the
    ordinary CSP/CC close functions above (or a plain equity sale this
    module does not itself model) before this is called."""
    updated = _apply_transition(wheel, WheelState.WHEEL_EXITED, reason=reason, now=now, exit_reason=reason)
    return _append_event(updated, WheelEventType.WHEEL_CLOSED, now=now, detail=f"exited: {reason}")
