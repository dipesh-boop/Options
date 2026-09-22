"""Step 22.2 Part 17/18/23: Wheel-specific Devil's Advocate / Portfolio
Manager review-context wiring tests."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.llm.devils_advocate import DevilsAdvocateInputs
from src.llm.portfolio_manager import PortfolioManagerInputs
from src.wheel import lifecycle
from src.wheel.review_context import (
    WHEEL_DEVILS_ADVOCATE_FAILURE_PROMPTS,
    WHEEL_PM_CC_PHASE_QUESTIONS,
    WHEEL_PM_CSP_PHASE_QUESTIONS,
    build_wheel_review_context,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestChecklistContent:
    def test_at_least_ten_failure_prompts(self):
        assert len(WHEEL_DEVILS_ADVOCATE_FAILURE_PROMPTS) >= 10

    def test_pm_question_sets_are_nonempty_and_distinct(self):
        assert len(WHEEL_PM_CSP_PHASE_QUESTIONS) >= 10
        assert len(WHEEL_PM_CC_PHASE_QUESTIONS) >= 5
        assert set(WHEEL_PM_CSP_PHASE_QUESTIONS) != set(WHEEL_PM_CC_PHASE_QUESTIONS)


class TestBuildWheelReviewContext:
    def test_new_candidate_gets_csp_phase_questions(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        ctx = build_wheel_review_context(w, is_new_wheel_candidate=True, now=NOW)
        assert ctx.portfolio_manager_questions == WHEEL_PM_CSP_PHASE_QUESTIONS
        assert ctx.is_new_wheel_candidate is True

    def test_covered_call_phase_gets_cc_phase_questions(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        w = lifecycle.mark_cc_eligible(w, now=NOW)
        ctx = build_wheel_review_context(w, is_new_wheel_candidate=False, now=NOW)
        assert ctx.portfolio_manager_questions == WHEEL_PM_CC_PHASE_QUESTIONS

    def test_below_basis_flags_propagate_from_the_open_cc_cycle(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        w = lifecycle.csp_assigned(w, now=NOW)
        w = lifecycle.mark_cc_eligible(w, now=NOW)
        w = lifecycle.open_cc(w, strike=45.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id=None, now=NOW, below_acquisition_basis=True, below_economic_basis=True, max_loss_if_called_away=500.0)
        ctx = build_wheel_review_context(w, is_new_wheel_candidate=False, now=NOW)
        assert ctx.below_acquisition_basis is True
        assert ctx.below_economic_basis is True

    def test_context_never_fabricates_basis_before_assignment(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        ctx = build_wheel_review_context(w, is_new_wheel_candidate=True, now=NOW)
        assert ctx.acquisition_basis_per_share is None
        assert ctx.economic_basis_per_share is None


class TestOptionalWiringOnInputs:
    def test_devils_advocate_inputs_defaults_to_no_wheel_context(self):
        # dataclass field default -- proves a standalone (non-Wheel)
        # proposal never silently gets Wheel context attached.
        assert DevilsAdvocateInputs.__dataclass_fields__["wheel_context"].default is None

    def test_portfolio_manager_inputs_defaults_to_no_wheel_context(self):
        assert PortfolioManagerInputs.__dataclass_fields__["wheel_context"].default is None
