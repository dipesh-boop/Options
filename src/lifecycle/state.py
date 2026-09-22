"""The canonical position lifecycle state machine (Part 2).

**Pre-fill states mirror `src.brokers.fidelity.TicketStatus`'s own
vocabulary** (`PROPOSED` through `REPRICE_REQUIRED`) rather than
importing/subclassing it: `TicketStatus` is part of the trusted-kernel
Fidelity module CLAUDE.md names explicitly, and this is a *separate*,
newer canonical enum spec Part 2 names in full (including 17 states that
only exist after a fill, which `TicketStatus` was never designed to
carry — a ticket's job ends at `AWAITING_HUMAN`/`ORDER_ENTERED`/
`FILLED`). `from_ticket_status()`/`TICKET_STATUS_MAP` below are the
single place that keeps the two vocabularies from ever silently
disagreeing about what e.g. `RISK_APPROVED` means, so a caller bridging
the two never has to hand-translate.

**`EXPIRED` here means the option contract itself expired** (an
`ExpirationSettlement`/`LegSettlement`-shaped event) — a different
concept from `TicketStatus.EXPIRED` (a ticket that was never entered in
time). The two enums use the same English word for two genuinely
different real-world events; they are never interchangeable, and no
function in this codebase converts one to the other.
"""
from __future__ import annotations

from enum import Enum


class PositionLifecycleState(str, Enum):
    # ---- pre-fill (mirrors TicketStatus's own vocabulary) ----
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
    REPRICE_REQUIRED = "reprice_required"

    # ---- post-fill: active management ----
    ACTIVE = "active"
    PROFIT_TARGET_REACHED = "profit_target_reached"
    LOSS_THRESHOLD_REACHED = "loss_threshold_reached"
    TIME_EXIT_TRIGGERED = "time_exit_triggered"
    DELTA_TRIGGERED = "delta_triggered"
    VOLATILITY_TRIGGERED = "volatility_triggered"
    REGIME_CHANGE_TRIGGERED = "regime_change_triggered"
    RISK_EXIT_REQUIRED = "risk_exit_required"
    ADJUSTMENT_CANDIDATE = "adjustment_candidate"
    EXIT_PENDING = "exit_pending"
    PARTIALLY_CLOSED = "partially_closed"
    CLOSED = "closed"
    EXPIRED = "expired"
    ASSIGNED = "assigned"
    CALLED_AWAY = "called_away"
    HALTED = "halted"
    DATA_INSUFFICIENT = "data_insufficient"


# Terminal: no outgoing edge anywhere. HALTED/DATA_INSUFFICIENT are
# deliberately NOT terminal -- both are recoverable once, respectively,
# a portfolio-level halt condition clears or fresh data arrives (the
# caller's responsibility to verify; this module only allows the edge).
TERMINAL_STATES: frozenset[PositionLifecycleState] = frozenset(
    {
        PositionLifecycleState.CANCELLED,
        PositionLifecycleState.REJECTED,
        PositionLifecycleState.CLOSED,
        PositionLifecycleState.EXPIRED,
        PositionLifecycleState.ASSIGNED,
        PositionLifecycleState.CALLED_AWAY,
    }
)

# Every *_TRIGGERED / RISK_EXIT_REQUIRED / ADJUSTMENT_CANDIDATE state a
# fresh lifecycle evaluation can land the position back in from ACTIVE,
# HALTED, or DATA_INSUFFICIENT -- built once here so the escape-hatch
# wiring below (and DATA_INSUFFICIENT's own recovery edges) can reuse it
# instead of repeating the same nine-member set four times.
_TRIGGER_STATES: frozenset[PositionLifecycleState] = frozenset(
    {
        PositionLifecycleState.PROFIT_TARGET_REACHED,
        PositionLifecycleState.LOSS_THRESHOLD_REACHED,
        PositionLifecycleState.TIME_EXIT_TRIGGERED,
        PositionLifecycleState.DELTA_TRIGGERED,
        PositionLifecycleState.VOLATILITY_TRIGGERED,
        PositionLifecycleState.REGIME_CHANGE_TRIGGERED,
        PositionLifecycleState.RISK_EXIT_REQUIRED,
        PositionLifecycleState.ADJUSTMENT_CANDIDATE,
    }
)

_S = PositionLifecycleState
_BASE_TRANSITIONS: dict[PositionLifecycleState, frozenset[PositionLifecycleState]] = {
    # -- pre-fill --
    _S.PROPOSED: frozenset({_S.QUANT_APPROVED, _S.REJECTED}),
    _S.QUANT_APPROVED: frozenset({_S.LLM_REVIEWED, _S.REJECTED}),
    _S.LLM_REVIEWED: frozenset({_S.RISK_APPROVED, _S.REJECTED}),
    _S.RISK_APPROVED: frozenset({_S.AWAITING_HUMAN, _S.REJECTED}),
    _S.AWAITING_HUMAN: frozenset({_S.ORDER_ENTERED, _S.REPRICE_REQUIRED, _S.CANCELLED, _S.REJECTED}),
    _S.ORDER_ENTERED: frozenset({_S.PARTIALLY_FILLED, _S.FILLED, _S.REPRICE_REQUIRED, _S.CANCELLED}),
    _S.PARTIALLY_FILLED: frozenset({_S.FILLED, _S.CANCELLED}),
    _S.REPRICE_REQUIRED: frozenset({_S.AWAITING_HUMAN, _S.CANCELLED, _S.REJECTED}),
    _S.CANCELLED: frozenset(),
    _S.REJECTED: frozenset(),
    # -- post-fill --
    _S.FILLED: frozenset({_S.ACTIVE}),
    _S.ACTIVE: _TRIGGER_STATES | frozenset({_S.EXPIRED, _S.ASSIGNED, _S.CALLED_AWAY}),
    _S.PROFIT_TARGET_REACHED: frozenset({_S.EXIT_PENDING, _S.ACTIVE}),
    _S.LOSS_THRESHOLD_REACHED: frozenset({_S.EXIT_PENDING, _S.RISK_EXIT_REQUIRED, _S.ACTIVE}),
    _S.TIME_EXIT_TRIGGERED: frozenset({_S.EXIT_PENDING, _S.ACTIVE}),
    _S.DELTA_TRIGGERED: frozenset({_S.EXIT_PENDING, _S.ADJUSTMENT_CANDIDATE, _S.ACTIVE}),
    _S.VOLATILITY_TRIGGERED: frozenset({_S.EXIT_PENDING, _S.ACTIVE}),
    _S.REGIME_CHANGE_TRIGGERED: frozenset({_S.EXIT_PENDING, _S.ACTIVE}),
    # RISK_EXIT_REQUIRED is mandatory -- no discretion to fall back to
    # ACTIVE (Part 18: "a lower-priority rule cannot override a
    # higher-priority safety action," and nothing outranks Risk).
    _S.RISK_EXIT_REQUIRED: frozenset({_S.EXIT_PENDING}),
    _S.ADJUSTMENT_CANDIDATE: frozenset({_S.ACTIVE, _S.EXIT_PENDING}),
    _S.EXIT_PENDING: frozenset({_S.PARTIALLY_CLOSED, _S.CLOSED, _S.CANCELLED}),
    _S.PARTIALLY_CLOSED: frozenset({_S.CLOSED, _S.EXIT_PENDING}),
    _S.CLOSED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.ASSIGNED: frozenset(),
    _S.CALLED_AWAY: frozenset(),
    # HALTED/DATA_INSUFFICIENT get their escape-hatch edges added below;
    # their own outgoing edges beyond that are empty here.
    _S.HALTED: frozenset(),
    _S.DATA_INSUFFICIENT: frozenset(),
}

# Escape hatches, added programmatically so they can never silently
# drift out of sync with _BASE_TRANSITIONS' own state list:
# - Every non-terminal, non-pre-fill state may transition to HALTED
#   (a portfolio-level Risk halt can interrupt monitoring at any point),
#   to DATA_INSUFFICIENT (fresh data can go stale/missing between any
#   two evaluations), and to RISK_EXIT_REQUIRED (Part 18: "nothing
#   outranks Risk" means a Risk Engine exit-required signal must be
#   reachable from *any* monitoring state -- e.g. DELTA_TRIGGERED or
#   PROFIT_TARGET_REACHED -- not only from ACTIVE; a position already
#   sitting in a softer trigger state is not shielded from a harder
#   Risk-mandated one).
# - HALTED may resume to ACTIVE (once the halt condition is externally
#   verified cleared -- this module only allows the edge, it never
#   decides the halt has lifted) or proceed to EXIT_PENDING (a halted
#   position can still be force-exited).
# - DATA_INSUFFICIENT may resume to ACTIVE or land directly in any
#   trigger state once fresh data arrives and is evaluated.
_POST_FILL_MONITORING_STATES = frozenset({_S.ACTIVE}) | _TRIGGER_STATES
_ESCAPE_HATCHES = frozenset({_S.HALTED, _S.DATA_INSUFFICIENT, _S.RISK_EXIT_REQUIRED})
VALID_TRANSITIONS: dict[PositionLifecycleState, frozenset[PositionLifecycleState]] = {
    state: (
        edges | (_ESCAPE_HATCHES - {state})
        if state in _POST_FILL_MONITORING_STATES
        else edges
    )
    for state, edges in _BASE_TRANSITIONS.items()
}
VALID_TRANSITIONS[_S.HALTED] = frozenset({_S.ACTIVE, _S.EXIT_PENDING})
VALID_TRANSITIONS[_S.DATA_INSUFFICIENT] = frozenset({_S.ACTIVE}) | _TRIGGER_STATES
# The generic escape-hatch comprehension above would otherwise also add
# a RISK_EXIT_REQUIRED -> HALTED edge (RISK_EXIT_REQUIRED is itself a
# member of _POST_FILL_MONITORING_STATES via _TRIGGER_STATES). Left in
# place, that edge would let a position bounce RISK_EXIT_REQUIRED ->
# HALTED -> ACTIVE, a two-hop route back to ACTIVE that defeats the
# "no discretion to fall back to ACTIVE" rule on RISK_EXIT_REQUIRED's
# own base transition. DATA_INSUFFICIENT is kept (a position mandated
# to exit still needs real data to exit safely -- if data goes stale
# first, that must escalate, never silently proceed on stale prices)
# but HALTED is deliberately excluded here.
VALID_TRANSITIONS[_S.RISK_EXIT_REQUIRED] = frozenset({_S.EXIT_PENDING, _S.DATA_INSUFFICIENT})


class InvalidLifecycleTransitionError(ValueError):
    """Raised by `transition()` for any state change not in
    `VALID_TRANSITIONS` -- the concrete mechanism behind Part 2's "no
    silent state transitions.\""""


def transition(current: PositionLifecycleState, target: PositionLifecycleState) -> PositionLifecycleState:
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidLifecycleTransitionError(
            f"illegal lifecycle transition {current.value!r} -> {target.value!r}; "
            f"from {current.value!r} only {sorted(s.value for s in allowed)} are allowed"
        )
    return target


def is_terminal(state: PositionLifecycleState) -> bool:
    return state in TERMINAL_STATES


# ---------------------------------------------------- TicketStatus bridge

_TICKET_STATUS_TO_LIFECYCLE: dict[str, PositionLifecycleState] = {
    "proposed": PositionLifecycleState.PROPOSED,
    "quant_approved": PositionLifecycleState.QUANT_APPROVED,
    "llm_reviewed": PositionLifecycleState.LLM_REVIEWED,
    "risk_approved": PositionLifecycleState.RISK_APPROVED,
    "awaiting_human": PositionLifecycleState.AWAITING_HUMAN,
    "order_entered": PositionLifecycleState.ORDER_ENTERED,
    "partially_filled": PositionLifecycleState.PARTIALLY_FILLED,
    "filled": PositionLifecycleState.FILLED,
    "cancelled": PositionLifecycleState.CANCELLED,
    "rejected": PositionLifecycleState.REJECTED,
    "reprice_required": PositionLifecycleState.REPRICE_REQUIRED,
    # TicketStatus.EXPIRED (a ticket never entered in time) has no
    # lifecycle counterpart -- deliberately mapped to CANCELLED, the
    # closest real lifecycle-terminal meaning ("this never became a
    # position"), never to PositionLifecycleState.EXPIRED (which means
    # the *option contract* expired, a wholly different event -- see
    # module docstring).
    "expired": PositionLifecycleState.CANCELLED,
}


def from_ticket_status(ticket_status_value: str) -> PositionLifecycleState:
    """Maps a `src.brokers.fidelity.TicketStatus` value (its `.value`,
    to avoid importing that module's enum type here and creating a
    circular/needless coupling) to the equivalent pre-fill
    `PositionLifecycleState`. Raises `KeyError` for anything unrecognized
    -- never guesses."""
    return _TICKET_STATUS_TO_LIFECYCLE[ticket_status_value]
