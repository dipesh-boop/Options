"""Part 17: the control loop's canonical recommendation vocabulary.

Distinct from (and a superset of, for this layer's purposes)
`src.lifecycle.state.PositionLifecycleState` -- that enum is the
Lifecycle Engine's own internal state machine (`PROPOSED` through
`CLOSED`/`EXPIRED`/`ASSIGNED`), built for Part 2's state transitions.
`ControlLoopAction` is the control loop's own outward-facing
recommendation label, covering both open-position monitoring (most of
which map 1:1 from a `src.lifecycle.precedence.ResolvedAction`, via
`action_from_lifecycle_category` below) AND decision points the
Lifecycle Engine never produces at all: a new-opportunity's Risk Engine
verdict (`RISK_REJECT`), a portfolio-wide halt (`PORTFOLIO_HALT`), a
stale pending Fidelity ticket (`REPRICE_REQUIRED`), or simply nothing to
do (`NO_ACTION`) -- those are constructed directly by whichever part of
the control loop makes that decision (Part 12's `run_control_cycle`),
not translated from anything.

**Structured action first, natural-language explanation second** (Part
17's own words): every `PortfolioControlDecisionSnapshot` (Part 30)
carries a `ControlLoopAction` value as its `recommended_action` field,
never a bare string an LLM could quietly redefine the meaning of.
"""
from __future__ import annotations

from enum import Enum

from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.state import PositionLifecycleState
from src.lifecycle.triggers import TriggerCategory


class ControlLoopAction(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    REDUCE = "reduce"
    EXIT_REQUIRED = "exit_required"
    PROFIT_TAKE = "profit_take"
    TIME_EXIT = "time_exit"
    REVIEW = "review"
    ADJUSTMENT_CANDIDATE = "adjustment_candidate"
    ROLL_CANDIDATE = "roll_candidate"
    ASSIGNMENT_REVIEW = "assignment_review"
    CALLED_AWAY = "called_away"
    REPRICE_REQUIRED = "reprice_required"
    DATA_INSUFFICIENT = "data_insufficient"
    RISK_REJECT = "risk_reject"
    PORTFOLIO_HALT = "portfolio_halt"
    NO_ACTION = "no_action"


# Part 18's exact 11-level order, restated in this vocabulary purely for
# display/sorting -- `src.lifecycle.precedence.resolve_action` remains
# the ONLY function that ever decides which category wins for a given
# position; this tuple never re-derives that decision, it only orders
# the resulting labels the same way, one entry per
# `src.lifecycle.triggers.PRECEDENCE_ORDER` category so the two can
# never silently drift apart from each other's ordering.
_CATEGORY_TO_ACTION: dict[TriggerCategory, ControlLoopAction] = {
    "system_data_safety": ControlLoopAction.DATA_INSUFFICIENT,
    "risk_halt": ControlLoopAction.PORTFOLIO_HALT,
    "hard_loss_exposure": ControlLoopAction.EXIT_REQUIRED,
    "assignment_expiration": ControlLoopAction.ASSIGNMENT_REVIEW,
    "event_risk": ControlLoopAction.REVIEW,
    "liquidity_risk": ControlLoopAction.REPRICE_REQUIRED,
    "time_exit": ControlLoopAction.TIME_EXIT,
    "profit_target": ControlLoopAction.PROFIT_TAKE,
    "delta_volatility_review": ControlLoopAction.ADJUSTMENT_CANDIDATE,
    "optional_adjustment": ControlLoopAction.ADJUSTMENT_CANDIDATE,
    "hold": ControlLoopAction.HOLD,
}

PRECEDENCE_DISPLAY_ORDER: tuple[ControlLoopAction, ...] = tuple(
    dict.fromkeys(_CATEGORY_TO_ACTION[c] for c in _CATEGORY_TO_ACTION)
)


def action_from_lifecycle_category(category: TriggerCategory) -> ControlLoopAction:
    """The one-to-one relabeling from a `ResolvedAction.category` (Part
    18's precedence winner) to this layer's vocabulary. Total over
    `TriggerCategory`'s 11 members -- a `KeyError` here means
    `src.lifecycle.triggers.TriggerCategory` grew a member this mapping
    doesn't know about yet, which must fail loudly (Part 6: never
    silently drop a category into a default), never fall back to
    `NO_ACTION`/`HOLD` and hide the gap."""
    return _CATEGORY_TO_ACTION[category]


def action_from_resolved(resolved: ResolvedAction) -> ControlLoopAction:
    """Convenience wrapper: `CALLED_AWAY` is the one case Part 17's
    vocabulary distinguishes more finely than a bare category-to-action
    mapping can -- a Wheel's own `SHARES_CALLED_AWAY` transition
    (src.wheel.state) is itself an `assignment_expiration`-category
    lifecycle trigger, but it's specifically the shares-being-called-away
    variant of that category, not a generic assignment review, so this
    checks `target_state` for that one refinement before falling back to
    the category table."""
    if resolved.target_state == PositionLifecycleState.CALLED_AWAY:
        return ControlLoopAction.CALLED_AWAY
    return action_from_lifecycle_category(resolved.category)
