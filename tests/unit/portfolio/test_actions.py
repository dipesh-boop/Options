"""Tests for `src.portfolio.actions` (Step 22.4 Part 17-18)."""
from __future__ import annotations

import pytest

from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.state import PositionLifecycleState
from src.lifecycle.triggers import PRECEDENCE_ORDER
from src.portfolio.actions import ControlLoopAction, action_from_lifecycle_category, action_from_resolved


def _resolved(category, target_state=PositionLifecycleState.ACTIVE) -> ResolvedAction:
    return ResolvedAction(
        category=category, target_state=target_state, mandatory=True, reason="x",
        winning_trigger_names=(), all_findings=(),
    )


class TestActionFromLifecycleCategory:
    def test_total_over_every_precedence_category(self):
        for category in PRECEDENCE_ORDER:
            result = action_from_lifecycle_category(category)
            assert isinstance(result, ControlLoopAction)

    def test_matches_part_18_precedence_ordering(self):
        expected = {
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
        for category, action in expected.items():
            assert action_from_lifecycle_category(category) == action

    def test_unknown_category_raises_rather_than_silently_defaulting(self):
        with pytest.raises(KeyError):
            action_from_lifecycle_category("not_a_real_category")  # type: ignore[arg-type]


class TestActionFromResolved:
    def test_called_away_refinement_overrides_generic_assignment_review(self):
        resolved = _resolved("assignment_expiration", target_state=PositionLifecycleState.CALLED_AWAY)
        assert action_from_resolved(resolved) == ControlLoopAction.CALLED_AWAY

    def test_generic_assignment_expiration_maps_to_assignment_review(self):
        resolved = _resolved("assignment_expiration", target_state=PositionLifecycleState.ASSIGNED)
        assert action_from_resolved(resolved) == ControlLoopAction.ASSIGNMENT_REVIEW

    def test_non_assignment_categories_unaffected_by_called_away_check(self):
        resolved = _resolved("profit_target", target_state=PositionLifecycleState.PROFIT_TARGET_REACHED)
        assert action_from_resolved(resolved) == ControlLoopAction.PROFIT_TAKE
