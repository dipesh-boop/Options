"""The Wheel's deterministic state machine: `WheelState` and the closed
set of transitions `transition()` allows. This is the concrete
enforcement mechanism behind CLAUDE.md's "no code anywhere automatically
initiates another CSP/CC" and the Step 22.2 instruction's explicit state
diagram — every state change anywhere in `src.wheel` (lifecycle,
PaperBroker event handling, persistence reload) goes through `transition()`,
never a bare field assignment, so an illegal jump (e.g. `CSP_EXPIRED`
straight to `CC_OPEN`, skipping assignment entirely) raises instead of
silently happening.

State diagram (exactly the one Step 22.2 specifies, plus the three
escape-hatch states it names without constraining their exact edges):

    WHEEL_CANDIDATE -> CSP_OPEN
    WHEEL_CANDIDATE -> WHEEL_REJECTED   (failed eligibility/risk before any order)

    CSP_OPEN -> CSP_EXPIRED             (expired worthless -- terminal, no assignment)
    CSP_OPEN -> CSP_CLOSED              (bought back early -- terminal, no assignment)
    CSP_OPEN -> ASSIGNED_SHARES         (assigned at expiration)

    ASSIGNED_SHARES -> CC_ELIGIBLE

    CC_ELIGIBLE -> CC_OPEN              (an acceptable covered call was found)
    (CC_ELIGIBLE has no forward transition for NO_CC_TRADE -- holding
     shares without selling a call is staying at CC_ELIGIBLE, not a
     distinct state; see `lifecycle.no_cc_trade`.)

    CC_OPEN -> CC_EXPIRED -> CC_ELIGIBLE            (repeat)
    CC_OPEN -> CC_CLOSED  -> CC_ELIGIBLE            (bought back, reassess)
    CC_OPEN -> SHARES_CALLED_AWAY -> WHEEL_COMPLETE (assigned on the call)

Escape hatches, reachable from any non-terminal state (a human or the
Risk Engine can always halt or exit a Wheel; only WHEEL_CANDIDATE can be
flatly rejected, since rejection means "never placed an order at all" —
once an order exists, unwinding it is an exit, not a rejection):

    <any non-terminal state> -> WHEEL_HALTED
    <any non-terminal state> -> WHEEL_EXITED
    WHEEL_HALTED -> WHEEL_EXITED        (a halted Wheel can still be unwound later)

Terminal states (no outgoing transition at all): CSP_EXPIRED, CSP_CLOSED,
WHEEL_COMPLETE, WHEEL_EXITED, WHEEL_REJECTED.
"""
from __future__ import annotations

from enum import Enum


class WheelState(str, Enum):
    WHEEL_CANDIDATE = "wheel_candidate"
    CSP_OPEN = "csp_open"
    CSP_EXPIRED = "csp_expired"
    CSP_CLOSED = "csp_closed"
    ASSIGNED_SHARES = "assigned_shares"
    CC_ELIGIBLE = "cc_eligible"
    CC_OPEN = "cc_open"
    CC_EXPIRED = "cc_expired"
    CC_CLOSED = "cc_closed"
    SHARES_CALLED_AWAY = "shares_called_away"
    WHEEL_COMPLETE = "wheel_complete"
    WHEEL_EXITED = "wheel_exited"
    WHEEL_HALTED = "wheel_halted"
    WHEEL_REJECTED = "wheel_rejected"


# States that own shares (used by risk/eligibility/accounting to know
# whether 100+ shares per contract must already be on the books).
SHARE_OWNING_STATES: frozenset[WheelState] = frozenset(
    {WheelState.ASSIGNED_SHARES, WheelState.CC_ELIGIBLE, WheelState.CC_OPEN, WheelState.CC_EXPIRED, WheelState.CC_CLOSED}
)

TERMINAL_STATES: frozenset[WheelState] = frozenset(
    {
        WheelState.CSP_EXPIRED,
        WheelState.CSP_CLOSED,
        WheelState.WHEEL_COMPLETE,
        WheelState.WHEEL_EXITED,
        WheelState.WHEEL_REJECTED,
    }
)

_S = WheelState
_BASE_TRANSITIONS: dict[WheelState, frozenset[WheelState]] = {
    _S.WHEEL_CANDIDATE: frozenset({_S.CSP_OPEN, _S.WHEEL_REJECTED}),
    _S.CSP_OPEN: frozenset({_S.CSP_EXPIRED, _S.CSP_CLOSED, _S.ASSIGNED_SHARES}),
    _S.CSP_EXPIRED: frozenset(),
    _S.CSP_CLOSED: frozenset(),
    _S.ASSIGNED_SHARES: frozenset({_S.CC_ELIGIBLE}),
    _S.CC_ELIGIBLE: frozenset({_S.CC_OPEN}),
    _S.CC_OPEN: frozenset({_S.CC_EXPIRED, _S.CC_CLOSED, _S.SHARES_CALLED_AWAY}),
    _S.CC_EXPIRED: frozenset({_S.CC_ELIGIBLE}),
    _S.CC_CLOSED: frozenset({_S.CC_ELIGIBLE}),
    _S.SHARES_CALLED_AWAY: frozenset({_S.WHEEL_COMPLETE}),
    _S.WHEEL_COMPLETE: frozenset(),
    _S.WHEEL_EXITED: frozenset(),
    _S.WHEEL_HALTED: frozenset({_S.WHEEL_EXITED}),
    _S.WHEEL_REJECTED: frozenset(),
}

# The escape hatches: every non-terminal state (including WHEEL_CANDIDATE)
# may additionally transition straight to WHEEL_HALTED or WHEEL_EXITED.
# Built once, from _BASE_TRANSITIONS' own terminal-state list, so the two
# can never silently drift apart.
VALID_TRANSITIONS: dict[WheelState, frozenset[WheelState]] = {
    state: (
        edges | frozenset({_S.WHEEL_HALTED, _S.WHEEL_EXITED})
        if state not in TERMINAL_STATES and state not in (_S.WHEEL_HALTED,)
        else edges
    )
    for state, edges in _BASE_TRANSITIONS.items()
}


class InvalidWheelTransitionError(ValueError):
    """Raised by `transition()` for any state change not in
    `VALID_TRANSITIONS` — the concrete mechanism preventing, e.g., a
    Wheel skipping assignment entirely or restarting a fresh CSP cycle
    without a new `wheel_id` (CLAUDE.md: "A new Wheel must compete again
    against all strategies and CASH")."""


def transition(current: WheelState, target: WheelState) -> WheelState:
    """Validates `current -> target` against `VALID_TRANSITIONS` and
    returns `target` if legal. Never mutates anything itself — callers
    (`src.wheel.lifecycle`) use this as the single choke point before
    constructing an updated `WheelPosition`, exactly as
    `src.risk.engine.evaluate_trade_proposal` is the single choke point
    for order approval."""
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidWheelTransitionError(
            f"illegal Wheel state transition {current.value!r} -> {target.value!r}; "
            f"from {current.value!r} only {sorted(s.value for s in allowed)} are allowed"
        )
    return target


def is_terminal(state: WheelState) -> bool:
    return state in TERMINAL_STATES
