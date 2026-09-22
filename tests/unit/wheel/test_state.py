"""Step 22.2 Part 23: deterministic state-machine tests for src.wheel.state."""
from __future__ import annotations

import pytest

from src.wheel.state import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidWheelTransitionError,
    WheelState,
    is_terminal,
    transition,
)


class TestValidTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (WheelState.WHEEL_CANDIDATE, WheelState.CSP_OPEN),
            (WheelState.WHEEL_CANDIDATE, WheelState.WHEEL_REJECTED),
            (WheelState.CSP_OPEN, WheelState.CSP_EXPIRED),
            (WheelState.CSP_OPEN, WheelState.CSP_CLOSED),
            (WheelState.CSP_OPEN, WheelState.ASSIGNED_SHARES),
            (WheelState.ASSIGNED_SHARES, WheelState.CC_ELIGIBLE),
            (WheelState.CC_ELIGIBLE, WheelState.CC_OPEN),
            (WheelState.CC_OPEN, WheelState.CC_EXPIRED),
            (WheelState.CC_OPEN, WheelState.CC_CLOSED),
            (WheelState.CC_OPEN, WheelState.SHARES_CALLED_AWAY),
            (WheelState.CC_EXPIRED, WheelState.CC_ELIGIBLE),
            (WheelState.CC_CLOSED, WheelState.CC_ELIGIBLE),
            (WheelState.SHARES_CALLED_AWAY, WheelState.WHEEL_COMPLETE),
            (WheelState.WHEEL_HALTED, WheelState.WHEEL_EXITED),
        ],
    )
    def test_named_edge_is_allowed(self, current, target):
        assert transition(current, target) == target

    @pytest.mark.parametrize(
        "current",
        [s for s in WheelState if s not in TERMINAL_STATES and s != WheelState.WHEEL_HALTED],
    )
    def test_every_nonterminal_state_can_halt_or_exit(self, current):
        assert transition(current, WheelState.WHEEL_HALTED) == WheelState.WHEEL_HALTED
        assert transition(current, WheelState.WHEEL_EXITED) == WheelState.WHEEL_EXITED


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (WheelState.CSP_EXPIRED, WheelState.CC_OPEN),
            (WheelState.CSP_CLOSED, WheelState.ASSIGNED_SHARES),
            (WheelState.WHEEL_CANDIDATE, WheelState.ASSIGNED_SHARES),
            (WheelState.WHEEL_CANDIDATE, WheelState.CC_OPEN),
            (WheelState.CSP_OPEN, WheelState.CC_OPEN),
            (WheelState.CSP_OPEN, WheelState.WHEEL_COMPLETE),
            (WheelState.ASSIGNED_SHARES, WheelState.CC_OPEN),  # must pass through CC_ELIGIBLE first
            (WheelState.CC_ELIGIBLE, WheelState.SHARES_CALLED_AWAY),
            (WheelState.CC_OPEN, WheelState.WHEEL_COMPLETE),  # must pass through SHARES_CALLED_AWAY first
            (WheelState.WHEEL_COMPLETE, WheelState.CSP_OPEN),  # terminal -- never restarts the same wheel_id
            (WheelState.WHEEL_EXITED, WheelState.CSP_OPEN),
            (WheelState.WHEEL_REJECTED, WheelState.CSP_OPEN),
            (WheelState.WHEEL_HALTED, WheelState.CSP_OPEN),  # halted cannot resume, only exit
            (WheelState.WHEEL_HALTED, WheelState.CC_OPEN),
        ],
    )
    def test_illegal_edge_raises(self, current, target):
        with pytest.raises(InvalidWheelTransitionError):
            transition(current, target)

    def test_every_terminal_state_has_no_outgoing_edges(self):
        for state in TERMINAL_STATES:
            assert VALID_TRANSITIONS[state] == frozenset(), f"{state} must be terminal (no outgoing edges)"


class TestIsTerminal:
    @pytest.mark.parametrize("state", list(TERMINAL_STATES))
    def test_terminal_states_report_terminal(self, state):
        assert is_terminal(state) is True

    @pytest.mark.parametrize("state", [s for s in WheelState if s not in TERMINAL_STATES])
    def test_nonterminal_states_report_not_terminal(self, state):
        assert is_terminal(state) is False


class TestStateMachineCompleteness:
    def test_every_wheelstate_member_has_a_transitions_entry(self):
        for state in WheelState:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_required_named_states_all_exist(self):
        required = {
            "WHEEL_CANDIDATE", "CSP_OPEN", "CSP_EXPIRED", "CSP_CLOSED", "ASSIGNED_SHARES",
            "CC_ELIGIBLE", "CC_OPEN", "CC_EXPIRED", "CC_CLOSED", "SHARES_CALLED_AWAY",
            "WHEEL_COMPLETE", "WHEEL_EXITED", "WHEEL_HALTED", "WHEEL_REJECTED",
        }
        assert required == {s.name for s in WheelState}
