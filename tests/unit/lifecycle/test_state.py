"""Part 2/27: the canonical `PositionLifecycleState` machine, including
Part 18's "nothing outranks Risk" encoded at the state-machine level
(RISK_EXIT_REQUIRED reachable from every trigger state, no discretion
back to ACTIVE, and no two-hop bypass through HALTED)."""
from __future__ import annotations

import pytest

from src.lifecycle.state import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidLifecycleTransitionError,
    PositionLifecycleState as S,
    from_ticket_status,
    is_terminal,
    transition,
)

_TRIGGER_STATES = (
    S.PROFIT_TARGET_REACHED, S.LOSS_THRESHOLD_REACHED, S.TIME_EXIT_TRIGGERED, S.DELTA_TRIGGERED,
    S.VOLATILITY_TRIGGERED, S.REGIME_CHANGE_TRIGGERED, S.ADJUSTMENT_CANDIDATE,
)


class TestStateInventory:
    def test_28_states_defined(self):
        assert len(list(S)) == 28

    def test_every_state_has_a_transitions_entry(self):
        for s in S:
            assert s in VALID_TRANSITIONS


class TestHappyPathTransitions:
    def test_fill_to_active(self):
        assert transition(S.FILLED, S.ACTIVE) == S.ACTIVE

    def test_active_to_profit_target_to_exit_pending_to_closed(self):
        assert transition(S.ACTIVE, S.PROFIT_TARGET_REACHED) == S.PROFIT_TARGET_REACHED
        assert transition(S.PROFIT_TARGET_REACHED, S.EXIT_PENDING) == S.EXIT_PENDING
        assert transition(S.EXIT_PENDING, S.CLOSED) == S.CLOSED

    def test_prefill_chain(self):
        assert transition(S.PROPOSED, S.QUANT_APPROVED) == S.QUANT_APPROVED
        assert transition(S.QUANT_APPROVED, S.LLM_REVIEWED) == S.LLM_REVIEWED
        assert transition(S.LLM_REVIEWED, S.RISK_APPROVED) == S.RISK_APPROVED
        assert transition(S.RISK_APPROVED, S.AWAITING_HUMAN) == S.AWAITING_HUMAN
        assert transition(S.AWAITING_HUMAN, S.ORDER_ENTERED) == S.ORDER_ENTERED
        assert transition(S.ORDER_ENTERED, S.FILLED) == S.FILLED


class TestTerminalStates:
    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
    def test_terminal_states_have_no_outgoing_edges(self, state):
        assert VALID_TRANSITIONS[state] == frozenset()
        assert is_terminal(state)

    def test_halted_and_data_insufficient_are_not_terminal(self):
        assert not is_terminal(S.HALTED)
        assert not is_terminal(S.DATA_INSUFFICIENT)

    def test_illegal_transition_from_terminal_raises(self):
        with pytest.raises(InvalidLifecycleTransitionError):
            transition(S.CLOSED, S.ACTIVE)


class TestEscapeHatches:
    @pytest.mark.parametrize("trigger_state", (S.ACTIVE,) + _TRIGGER_STATES)
    def test_halted_reachable_from_every_monitoring_state(self, trigger_state):
        assert transition(trigger_state, S.HALTED) == S.HALTED

    @pytest.mark.parametrize("trigger_state", (S.ACTIVE,) + _TRIGGER_STATES)
    def test_data_insufficient_reachable_from_every_monitoring_state(self, trigger_state):
        assert transition(trigger_state, S.DATA_INSUFFICIENT) == S.DATA_INSUFFICIENT

    def test_halted_resumes_to_active_or_exit_pending(self):
        assert transition(S.HALTED, S.ACTIVE) == S.ACTIVE
        assert transition(S.HALTED, S.EXIT_PENDING) == S.EXIT_PENDING

    def test_data_insufficient_resumes_to_active_or_any_trigger_state(self):
        assert transition(S.DATA_INSUFFICIENT, S.ACTIVE) == S.ACTIVE
        assert transition(S.DATA_INSUFFICIENT, S.LOSS_THRESHOLD_REACHED) == S.LOSS_THRESHOLD_REACHED


class TestRiskExitRequiredHasNoDiscretion:
    @pytest.mark.parametrize("trigger_state", _TRIGGER_STATES)
    def test_risk_exit_required_reachable_from_every_trigger_state(self, trigger_state):
        """Part 18: RISK HALT outranks a position already sitting in a
        softer trigger state (e.g. PROFIT_TARGET_REACHED) -- not only
        reachable from ACTIVE."""
        assert transition(trigger_state, S.RISK_EXIT_REQUIRED) == S.RISK_EXIT_REQUIRED

    def test_risk_exit_required_cannot_fall_back_to_active(self):
        with pytest.raises(InvalidLifecycleTransitionError):
            transition(S.RISK_EXIT_REQUIRED, S.ACTIVE)

    def test_risk_exit_required_cannot_reach_halted(self):
        """Regression: RISK_EXIT_REQUIRED -> HALTED -> ACTIVE would be a
        two-hop bypass of the 'no discretion back to ACTIVE' rule."""
        with pytest.raises(InvalidLifecycleTransitionError):
            transition(S.RISK_EXIT_REQUIRED, S.HALTED)

    def test_risk_exit_required_can_still_degrade_to_data_insufficient(self):
        """A mandated exit still needs real data to execute safely."""
        assert transition(S.RISK_EXIT_REQUIRED, S.DATA_INSUFFICIENT) == S.DATA_INSUFFICIENT

    def test_risk_exit_required_only_forward_path_is_exit_pending(self):
        assert transition(S.RISK_EXIT_REQUIRED, S.EXIT_PENDING) == S.EXIT_PENDING


class TestTicketStatusBridge:
    @pytest.mark.parametrize(
        "ticket_value,expected",
        [
            ("proposed", S.PROPOSED),
            ("quant_approved", S.QUANT_APPROVED),
            ("llm_reviewed", S.LLM_REVIEWED),
            ("risk_approved", S.RISK_APPROVED),
            ("awaiting_human", S.AWAITING_HUMAN),
            ("order_entered", S.ORDER_ENTERED),
            ("partially_filled", S.PARTIALLY_FILLED),
            ("filled", S.FILLED),
            ("cancelled", S.CANCELLED),
            ("rejected", S.REJECTED),
            ("reprice_required", S.REPRICE_REQUIRED),
        ],
    )
    def test_bridges_to_matching_lifecycle_state(self, ticket_value, expected):
        assert from_ticket_status(ticket_value) == expected

    def test_ticket_expired_maps_to_cancelled_never_to_lifecycle_expired(self):
        """TicketStatus.EXPIRED means 'never entered in time' -- a
        wholly different event from PositionLifecycleState.EXPIRED
        (the option contract itself expired)."""
        assert from_ticket_status("expired") == S.CANCELLED

    def test_unrecognized_value_raises_keyerror(self):
        with pytest.raises(KeyError):
            from_ticket_status("not_a_real_status")
