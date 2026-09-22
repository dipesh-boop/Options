"""Part 13: the Rolling Engine -- a roll modeled as CLOSE + OPEN, never
one transaction, with cumulative loss tracked honestly across a chain."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.lifecycle.policy import ManagementPolicy
from src.lifecycle.rolling import (
    RollAlreadyCompletedError,
    RollNotPermittedError,
    complete_roll,
    new_roll_chain_id,
    record_roll_close,
)
from src.strategies.base import StrategyKind

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _policy(roll_allowed: bool) -> ManagementPolicy:
    return ManagementPolicy(name="X", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, description="test", roll_allowed=roll_allowed)


class TestRollPermission:
    def test_roll_not_allowed_raises(self):
        with pytest.raises(RollNotPermittedError):
            record_roll_close(
                policy=_policy(False), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
                realized_pnl_on_close=-50.0, close_cash_flow=-30.0, prior_chain_realized_pnl=0.0, reason="test",
            )


class TestRollChainId:
    def test_deterministic_and_stable(self):
        assert new_roll_chain_id("T1") == new_roll_chain_id("T1")

    def test_distinct_for_distinct_seeds(self):
        assert new_roll_chain_id("T1") != new_roll_chain_id("T2")


class TestRecordRollClose:
    def test_records_close_with_no_open_yet(self):
        rec = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-80.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="defending delta",
        )
        assert rec.roll_to_trade_id is None
        assert rec.net_roll_credit_debit is None
        assert rec.total_realized_loss_before_roll == -80.0

    def test_cumulative_loss_across_chain(self):
        rec1 = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-80.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="first roll",
        )
        rec2 = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T2", as_of=T0 + timedelta(days=10),
            realized_pnl_on_close=-40.0, close_cash_flow=-20.0, prior_chain_realized_pnl=rec1.total_realized_loss_before_roll,
            reason="second roll",
        )
        assert rec2.total_realized_loss_before_roll == -120.0

    def test_loss_is_never_hidden_by_a_profitable_looking_roll(self):
        """A large loss on close stays recorded even if the roll's net
        credit/debit later looks favorable."""
        rec = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-500.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="big loss",
        )
        completed = complete_roll(rec, roll_to_trade_id="T2", open_cash_flow=200.0)
        assert rec.realized_pnl_on_close == -500.0  # unchanged by the completion
        assert completed.realized_pnl_on_close == -500.0
        assert completed.net_roll_credit_debit == 150.0  # -50 + 200, a separate figure


class TestCompleteRoll:
    def test_sets_open_leg_and_net_credit_debit(self):
        rec = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-80.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="test",
        )
        completed = complete_roll(rec, roll_to_trade_id="T2", open_cash_flow=60.0)
        assert completed.roll_to_trade_id == "T2"
        assert completed.net_roll_credit_debit == 10.0

    def test_cannot_complete_twice(self):
        rec = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-80.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="test",
        )
        completed = complete_roll(rec, roll_to_trade_id="T2", open_cash_flow=60.0)
        with pytest.raises(RollAlreadyCompletedError):
            complete_roll(completed, roll_to_trade_id="T3", open_cash_flow=0.0)

    def test_record_is_frozen_never_mutated(self):
        rec = record_roll_close(
            policy=_policy(True), roll_chain_id="c1", roll_from_trade_id="T1", as_of=T0,
            realized_pnl_on_close=-80.0, close_cash_flow=-50.0, prior_chain_realized_pnl=0.0, reason="test",
        )
        completed = complete_roll(rec, roll_to_trade_id="T2", open_cash_flow=60.0)
        assert rec.roll_to_trade_id is None  # original untouched
        assert completed.roll_to_trade_id == "T2"
