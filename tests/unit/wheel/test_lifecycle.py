"""Step 22.2 Part 23: end-to-end Wheel lifecycle tests, including
assignment scenarios (ITM/OTM expiration, called-away accounting),
uncovered-call prevention, and halt/exit/reject escape hatches."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.wheel import lifecycle
from src.wheel.models import CcCloseReason, CspCloseReason
from src.wheel.state import WheelState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EXP1 = date(2026, 2, 1)
EXP2 = date(2026, 3, 1)


def _later(days: int) -> datetime:
    return NOW + timedelta(days=days)


class TestCandidateToCspOpen:
    def test_open_csp_transitions_and_records_cycle(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        assert w.state == WheelState.WHEEL_CANDIDATE
        w = lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)
        assert w.state == WheelState.CSP_OPEN
        assert w.csp_cycle_count == 1
        assert w.open_csp_cycle.proposal_id == "p1"
        assert w.events[-1].event_type.value == "csp_opened"

    def test_reject_candidate_only_from_candidate_state(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.reject_candidate(w, reason="failed eligibility", now=NOW)
        assert w.state == WheelState.WHEEL_REJECTED
        assert w.rejection_reason == "failed eligibility"
        assert w.completed_at == NOW

    def test_open_csp_requires_positive_contracts(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        with pytest.raises(ValueError):
            lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=0, premium_per_share=1.2, commission=0.65, proposal_id=None, position_id=None, now=NOW)


class TestCspUnassignedPaths:
    def _opened(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        return lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)

    def test_csp_expires_worthless_is_terminal_and_realizes_full_premium(self):
        w = self._opened()
        w = lifecycle.csp_expires_worthless(w, now=_later(31))
        assert w.state == WheelState.CSP_EXPIRED
        assert w.completed_at == _later(31)
        cycle = w.csp_cycles[0]
        assert cycle.close_reason == CspCloseReason.EXPIRED_WORTHLESS
        assert cycle.realized_pnl == pytest.approx(1.2 * 100 - 0.65)
        assert w.accounting.capital_committed == 0.0

    def test_csp_bought_to_close_realizes_premium_minus_buyback(self):
        w = self._opened()
        w = lifecycle.csp_bought_to_close(w, buyback_price_per_share=0.4, commission=0.65, now=_later(10))
        assert w.state == WheelState.CSP_CLOSED
        cycle = w.csp_cycles[0]
        assert cycle.close_reason == CspCloseReason.BOUGHT_TO_CLOSE
        assert cycle.realized_pnl == pytest.approx((1.2 - 0.4) * 100 - 1.30)  # entry + exit commission

    def test_terminal_unassigned_wheel_never_reopens_automatically(self):
        w = self._opened()
        w = lifecycle.csp_expires_worthless(w, now=_later(31))
        # No lifecycle function can move a terminal Wheel anywhere else --
        # proven by state.py's own transition table (see test_state.py),
        # re-verified here at the lifecycle layer for this exact path.
        with pytest.raises(Exception):
            lifecycle.open_csp(w, strike=50.0, expiration=EXP2, contracts=1, premium_per_share=1.0, commission=0.65, proposal_id="p2", position_id=None, now=_later(32))

    def test_closing_with_no_open_cycle_raises(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        with pytest.raises(ValueError):
            lifecycle.csp_expires_worthless(w, now=NOW)


class TestAssignmentAndCoveredCallCycle:
    def _assigned_wheel(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)
        w = lifecycle.csp_assigned(w, now=_later(31))
        return w

    def test_assignment_creates_100_shares_per_contract(self):
        w = self._assigned_wheel()
        assert w.state == WheelState.ASSIGNED_SHARES
        assert w.accounting.shares_owned == 100
        assert w.accounting.acquisition_basis_per_share == 50.0
        event_types = [e.event_type.value for e in w.events]
        assert "csp_assigned" in event_types
        assert "stock_position_created" in event_types

    def test_cc_eligible_then_no_cc_trade_stays_at_cc_eligible(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        assert w.state == WheelState.CC_ELIGIBLE
        w2 = lifecycle.no_cc_trade(w, reason="no acceptable strike found", now=_later(32))
        assert w2.state == WheelState.CC_ELIGIBLE  # holding shares without a call is valid
        assert w2.events[-1].event_type.value == "no_cc_trade"

    def test_no_cc_trade_rejected_outside_cc_eligible(self):
        w = self._assigned_wheel()  # still ASSIGNED_SHARES, not yet CC_ELIGIBLE
        with pytest.raises(ValueError):
            lifecycle.no_cc_trade(w, reason="x", now=_later(31))

    def test_uncovered_call_is_refused(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w2", ticker="AAPL", now=NOW)
        with pytest.raises(lifecycle.UncoveredCallError):
            lifecycle.open_cc(
                w, strike=100.0, expiration=EXP1, contracts=1, premium_per_share=1.0, commission=0.65,
                proposal_id=None, position_id=None, now=NOW, below_acquisition_basis=False,
                below_economic_basis=False, max_loss_if_called_away=None,
            )

    def test_open_cc_with_exactly_enough_shares_succeeds(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(
            w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65,
            proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False,
            below_economic_basis=False, max_loss_if_called_away=None,
        )
        assert w.state == WheelState.CC_OPEN
        assert w.cc_cycle_count == 1

    def test_below_basis_flag_is_carried_onto_the_cycle_and_events(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(
            w, strike=45.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65,
            proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=True,
            below_economic_basis=True, max_loss_if_called_away=500.0,
        )
        assert w.open_cc_cycle.below_acquisition_basis is True
        assert w.open_cc_cycle.below_economic_basis is True
        assert "BELOW_ACQUISITION_BASIS" in w.events[-1].detail

    def test_cc_expires_worthless_then_back_to_eligible(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        w = lifecycle.cc_expires_worthless(w, now=_later(59))
        assert w.state == WheelState.CC_EXPIRED
        cycle = w.cc_cycles[0]
        assert cycle.close_reason == CcCloseReason.EXPIRED_WORTHLESS
        w = lifecycle.mark_cc_eligible(w, now=_later(59))
        assert w.state == WheelState.CC_ELIGIBLE
        assert w.accounting.shares_owned == 100  # shares never touched by an unassigned CC cycle

    def test_cc_bought_to_close_then_reassess(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        w = lifecycle.cc_bought_to_close(w, buyback_price_per_share=0.3, commission=0.65, now=_later(40))
        assert w.state == WheelState.CC_CLOSED
        cycle = w.cc_cycles[0]
        assert cycle.realized_pnl == pytest.approx((0.8 - 0.3) * 100 - 1.30)

    def test_shares_called_away_realizes_stock_pnl_and_completes(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        w = lifecycle.shares_called_away(w, now=_later(59))
        assert w.state == WheelState.SHARES_CALLED_AWAY
        assert w.accounting.realized_stock_pnl == 300.0  # (53-50)*100
        assert w.accounting.shares_owned == 0
        w = lifecycle.complete_wheel(w, now=_later(59))
        assert w.state == WheelState.WHEEL_COMPLETE
        assert w.completed_at == _later(59)

    def test_multiple_cc_cycles_before_called_away_all_recorded(self):
        w = self._assigned_wheel()
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        w = lifecycle.cc_expires_worthless(w, now=_later(59))
        w = lifecycle.mark_cc_eligible(w, now=_later(59))
        w = lifecycle.open_cc(w, strike=54.0, expiration=date(2026, 4, 1), contracts=1, premium_per_share=0.7, commission=0.65, proposal_id="p3", position_id="pos3", now=_later(59), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        w = lifecycle.shares_called_away(w, now=_later(90))
        assert w.cc_cycle_count == 2
        # 300 gain against basis realized only once, on the final call-away
        assert w.accounting.realized_stock_pnl == 400.0  # (54-50)*100


class TestEscapeHatches:
    def test_halt_from_open_csp(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)
        w = lifecycle.halt_wheel(w, reason="drawdown halt", now=_later(5))
        assert w.state == WheelState.WHEEL_HALTED
        assert w.halt_reason == "drawdown halt"

    def test_halted_wheel_can_still_be_exited(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.halt_wheel(w, reason="x", now=NOW)
        w = lifecycle.exit_wheel(w, reason="liquidated manually", now=_later(1))
        assert w.state == WheelState.WHEEL_EXITED

    def test_exit_from_candidate(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        w = lifecycle.exit_wheel(w, reason="changed mind", now=NOW)
        assert w.state == WheelState.WHEEL_EXITED
        assert w.exit_reason == "changed mind"


class TestWheelIdIntegrityAcrossEverything:
    def test_every_cycle_event_and_transition_carries_the_same_wheel_id(self):
        w = lifecycle.open_wheel_candidate(wheel_id="wheel-xyz", ticker="SPY", now=NOW)
        w = lifecycle.open_csp(w, strike=50.0, expiration=EXP1, contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)
        w = lifecycle.csp_assigned(w, now=_later(31))
        w = lifecycle.mark_cc_eligible(w, now=_later(31))
        w = lifecycle.open_cc(w, strike=53.0, expiration=EXP2, contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=_later(31), below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
        assert all(c.wheel_id == "wheel-xyz" for c in w.csp_cycles)
        assert all(c.wheel_id == "wheel-xyz" for c in w.cc_cycles)
        assert all(e.wheel_id == "wheel-xyz" for e in w.events)
        assert all(h.wheel_id == "wheel-xyz" for h in w.state_history)
