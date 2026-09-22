"""Part 18: the explicit 11-level action-precedence hierarchy.

1. SYSTEM/DATA SAFETY  2. RISK HALT  3. HARD LOSS/EXPOSURE RULE
4. ASSIGNMENT/EXPIRATION REQUIREMENT  5. EVENT RISK  6. LIQUIDITY RISK
7. TIME EXIT  8. PROFIT TARGET  9. DELTA/VOLATILITY REVIEW
10. OPTIONAL ADJUSTMENT  11. HOLD

"A lower-priority rule cannot override a higher-priority safety
action." This module is the one and only place that resolves a set of
independently-fired `TriggerFinding`s (`src.lifecycle.triggers`) into a
single deterministic action: whichever category is highest in
`triggers.PRECEDENCE_ORDER` and has at least one finding wins, full
stop — a lower category's finding is never allowed to be chosen over a
higher category's, even if the higher category's own finding is only
`mandatory=False` (a review, not a forced exit). Within a winning
category, `mandatory=True` findings are preferred over `mandatory=False`
ones so a hard rule in a category is never masked by a softer one in
the same category (e.g. `delta_close_threshold` over `delta_threshold`,
both category `delta_volatility_review`).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.lifecycle.state import PositionLifecycleState
from src.lifecycle.triggers import PRECEDENCE_ORDER, TriggerCategory, TriggerFinding


@dataclass(frozen=True)
class ResolvedAction:
    """The single deterministic outcome of one lifecycle evaluation.
    `all_findings` is retained in full (not just the winner) so the
    `LifecycleDecisionSnapshot` (Part 17) can record every candidate
    action a position had, not only the one that was acted on."""

    category: TriggerCategory
    target_state: PositionLifecycleState
    mandatory: bool
    reason: str
    winning_trigger_names: tuple[str, ...]
    all_findings: tuple[TriggerFinding, ...]


def resolve_action(findings: list[TriggerFinding], *, risk_halt_active: bool) -> ResolvedAction:
    """`risk_halt_active` is the portfolio-level Risk Engine/kill-switch
    signal (`src.risk.kill_switch`), passed in by the caller — this
    module never queries Risk itself, it only obeys the boolean it's
    handed, which is exactly what makes Part 18's "nothing outranks
    Risk" auditable: the boolean is either True (and RISK HALT wins
    over everything below it) or False (and it plays no further part)."""
    all_findings = tuple(findings)

    data_safety = [f for f in all_findings if f.category == "system_data_safety"]
    if data_safety:
        return ResolvedAction(
            category="system_data_safety",
            target_state=PositionLifecycleState.DATA_INSUFFICIENT,
            mandatory=True,
            reason="; ".join(sorted(f.reason for f in data_safety)),
            winning_trigger_names=tuple(sorted(f.trigger_name for f in data_safety)),
            all_findings=all_findings,
        )

    if risk_halt_active:
        return ResolvedAction(
            category="risk_halt",
            target_state=PositionLifecycleState.RISK_EXIT_REQUIRED,
            mandatory=True,
            reason="portfolio Risk Engine halt is active -- no lifecycle policy may override it",
            winning_trigger_names=(),
            all_findings=all_findings,
        )

    for category in PRECEDENCE_ORDER:
        if category in ("system_data_safety", "risk_halt", "hold"):
            continue
        in_category = [f for f in all_findings if f.category == category]
        if not in_category:
            continue
        mandatory_findings = [f for f in in_category if f.mandatory]
        chosen = sorted(mandatory_findings or in_category, key=lambda f: f.trigger_name)
        winner = chosen[0]
        return ResolvedAction(
            category=category,
            target_state=winner.target_state,
            mandatory=winner.mandatory,
            reason=winner.reason,
            winning_trigger_names=tuple(f.trigger_name for f in chosen),
            all_findings=all_findings,
        )

    return ResolvedAction(
        category="hold",
        target_state=PositionLifecycleState.ACTIVE,
        mandatory=False,
        reason="no lifecycle trigger fired",
        winning_trigger_names=(),
        all_findings=all_findings,
    )
