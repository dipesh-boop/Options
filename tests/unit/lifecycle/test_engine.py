"""The main orchestrator: triggers + precedence + excursion + state
machine + snapshot, tied together into one deterministic evaluation.
Covers Part 18's "nothing outranks Risk" end to end and Part 19's
DATA_INSUFFICIENT fail-safe end to end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.lifecycle.engine import PositionMonitoringInput, evaluate_position
from src.lifecycle.excursion import initial_excursion
from src.lifecycle.policy import ManagementPolicy
from src.lifecycle.state import PositionLifecycleState as S
from src.strategies.base import StrategyKind

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _policy(**overrides) -> ManagementPolicy:
    base = dict(
        name="PUT_CREDIT_SPREAD_STANDARD", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, description="test",
        profit_target_pct=0.5, management_dte=28, forced_exit_dte=21, max_loss_multiple_of_credit=1.5,
        delta_threshold=0.35, delta_close_threshold=0.5,
    )
    base.update(overrides)
    return ManagementPolicy(**base)


class TestEvaluatePosition:
    def test_hold_when_no_trigger_fires(self):
        policy = _policy()
        exc = initial_excursion(0.0, T0)
        inp = PositionMonitoringInput(as_of=T0 + timedelta(days=1), dte=40, unrealized_pnl=10.0, profit_capture_denominator=100.0, initial_credit=100.0, position_delta_abs=0.15)
        res = evaluate_position(
            trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD,
            policy=policy, current_state=S.ACTIVE, excursion=exc, inp=inp,
        )
        assert res.new_state == S.ACTIVE
        assert res.resolved.category == "hold"
        assert res.snapshot.current_state == S.ACTIVE

    def test_profit_target_reached_transitions_state(self):
        policy = _policy()
        exc = initial_excursion(0.0, T0)
        inp = PositionMonitoringInput(as_of=T0 + timedelta(days=5), dte=35, unrealized_pnl=60.0, profit_capture_denominator=100.0, initial_credit=100.0, position_delta_abs=0.15)
        res = evaluate_position(
            trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD,
            policy=policy, current_state=S.ACTIVE, excursion=exc, inp=inp,
        )
        assert res.new_state == S.PROFIT_TARGET_REACHED
        assert res.excursion.mfe == 60.0

    def test_risk_halt_overrides_profit_target_from_a_trigger_state(self):
        policy = _policy()
        exc = initial_excursion(60.0, T0)
        inp = PositionMonitoringInput(as_of=T0 + timedelta(days=6), dte=34, unrealized_pnl=65.0, profit_capture_denominator=100.0, initial_credit=100.0, position_delta_abs=0.15, risk_halt_active=True)
        res = evaluate_position(
            trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD,
            policy=policy, current_state=S.PROFIT_TARGET_REACHED, excursion=exc, inp=inp,
        )
        assert res.new_state == S.RISK_EXIT_REQUIRED
        assert res.resolved.category == "risk_halt"

    def test_data_insufficient_outranks_risk_halt(self):
        policy = _policy()
        exc = initial_excursion(60.0, T0)
        inp = PositionMonitoringInput(as_of=T0 + timedelta(days=7), dte=33, unrealized_pnl=10.0, profit_capture_denominator=100.0, initial_credit=100.0, position_delta_abs=None, risk_halt_active=True)
        res = evaluate_position(
            trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD,
            policy=policy, current_state=S.RISK_EXIT_REQUIRED, excursion=exc, inp=inp,
        )
        assert res.new_state == S.DATA_INSUFFICIENT
        assert res.resolved.category == "system_data_safety"

    def test_llm_cannot_override_deterministic_action(self):
        """There is no parameter on evaluate_position for an LLM
        opinion to influence the resolved action or the resulting
        state transition -- the function signature itself is the
        proof: only Python-computed PositionMonitoringInput figures
        and the policy drive the outcome."""
        import inspect

        sig = inspect.signature(evaluate_position)
        for name in sig.parameters:
            assert "llm" not in name.lower()
            assert "advocate" not in name.lower()
            assert "opinion" not in name.lower()

    def test_mismatched_strategy_kind_raises(self):
        policy = _policy()
        exc = initial_excursion(0.0, T0)
        inp = PositionMonitoringInput(as_of=T0, dte=40, unrealized_pnl=0.0)
        with pytest.raises(ValueError):
            evaluate_position(
                trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.LONG_CALL,
                policy=policy, current_state=S.ACTIVE, excursion=exc, inp=inp,
            )

    def test_evaluation_is_pure_and_deterministic(self):
        """Same inputs -> same outputs, every time (Part 27's backtest
        determinism requirement)."""
        policy = _policy()
        exc = initial_excursion(0.0, T0)
        inp = PositionMonitoringInput(as_of=T0 + timedelta(days=5), dte=35, unrealized_pnl=60.0, profit_capture_denominator=100.0, initial_credit=100.0, position_delta_abs=0.15)
        r1 = evaluate_position(trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD, policy=policy, current_state=S.ACTIVE, excursion=exc, inp=inp)
        r2 = evaluate_position(trade_id="T1", wheel_id=None, strategy_kind_for_snapshot=StrategyKind.PUT_CREDIT_SPREAD, policy=policy, current_state=S.ACTIVE, excursion=exc, inp=inp)
        assert r1.new_state == r2.new_state
        assert r1.resolved.reason == r2.resolved.reason
        assert r1.excursion == r2.excursion

    def test_naive_as_of_rejected(self):
        with pytest.raises(Exception):
            PositionMonitoringInput(as_of=datetime(2026, 1, 1), dte=40, unrealized_pnl=0.0)
