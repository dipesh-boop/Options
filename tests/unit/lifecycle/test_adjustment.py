"""Part 14: the Adjustment Engine -- a closed set of named
transformations, each carrying deterministic before/after exposure.
Producing a proposal is not approval; Risk still evaluates it."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.lifecycle.adjustment import (
    AdjustmentNotPermittedError,
    AdjustmentType,
    propose_adjustment,
)
from src.lifecycle.policy import ManagementPolicy
from src.strategies.base import StrategyKind

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _policy(adjustment_allowed: bool) -> ManagementPolicy:
    return ManagementPolicy(name="X", strategy_kind=StrategyKind.SHORT_IRON_CONDOR, description="test", adjustment_allowed=adjustment_allowed)


class TestAdjustmentPermission:
    def test_not_permitted_raises(self):
        with pytest.raises(AdjustmentNotPermittedError):
            propose_adjustment(
                _policy(False), trade_id="T1", adjustment_type=AdjustmentType.CLOSE_UNCHALLENGED_SIDE, as_of=T0,
                reason="test", incremental_cash_flow=10.0, before_max_loss=500.0, after_max_loss=300.0,
                before_capital_requirement=500.0, after_capital_requirement=300.0,
            )


class TestCloseUnchallengedSide:
    """Part 14's worked example: Iron Condor -> close the unchallenged
    side, reducing max loss and capital requirement."""

    def test_builds_proposal_with_before_after_exposure(self):
        adj = propose_adjustment(
            _policy(True), trade_id="T1", adjustment_type=AdjustmentType.CLOSE_UNCHALLENGED_SIDE, as_of=T0,
            reason="call side unchallenged, capital better used elsewhere", incremental_cash_flow=15.0,
            before_max_loss=500.0, after_max_loss=300.0, before_capital_requirement=500.0, after_capital_requirement=300.0,
            before_breakeven=95.0, after_breakeven=97.0,
        )
        assert adj.adjustment_type == AdjustmentType.CLOSE_UNCHALLENGED_SIDE
        assert adj.after_max_loss < adj.before_max_loss
        assert adj.after_capital_requirement < adj.before_capital_requirement
        assert adj.incremental_cash_flow == 15.0


class TestValidation:
    def test_negative_max_loss_rejected(self):
        with pytest.raises(ValidationError):
            propose_adjustment(
                _policy(True), trade_id="T1", adjustment_type=AdjustmentType.REDUCE_SIZE, as_of=T0, reason="x",
                incremental_cash_flow=0.0, before_max_loss=-5.0, after_max_loss=5.0,
                before_capital_requirement=5.0, after_capital_requirement=5.0,
            )

    def test_negative_capital_requirement_rejected(self):
        with pytest.raises(ValidationError):
            propose_adjustment(
                _policy(True), trade_id="T1", adjustment_type=AdjustmentType.REDUCE_SIZE, as_of=T0, reason="x",
                incremental_cash_flow=0.0, before_max_loss=5.0, after_max_loss=5.0,
                before_capital_requirement=-5.0, after_capital_requirement=5.0,
            )

    def test_naive_as_of_rejected(self):
        with pytest.raises(ValidationError):
            from src.lifecycle.adjustment import AdjustmentProposal

            AdjustmentProposal(
                trade_id="T1", adjustment_type=AdjustmentType.REDUCE_SIZE, as_of=datetime(2026, 1, 1), reason="x",
                incremental_cash_flow=0.0, before_max_loss=5.0, after_max_loss=5.0,
                before_capital_requirement=5.0, after_capital_requirement=5.0,
            )

    def test_frozen(self):
        adj = propose_adjustment(
            _policy(True), trade_id="T1", adjustment_type=AdjustmentType.REDUCE_SIZE, as_of=T0, reason="x",
            incremental_cash_flow=0.0, before_max_loss=5.0, after_max_loss=5.0,
            before_capital_requirement=5.0, after_capital_requirement=5.0,
        )
        with pytest.raises(ValidationError):
            adj.after_max_loss = 999.0


class TestAllAdjustmentTypesConstructible:
    @pytest.mark.parametrize("kind", list(AdjustmentType))
    def test_each_type_builds_a_valid_proposal(self, kind):
        adj = propose_adjustment(
            _policy(True), trade_id="T1", adjustment_type=kind, as_of=T0, reason="x", incremental_cash_flow=0.0,
            before_max_loss=100.0, after_max_loss=100.0, before_capital_requirement=100.0, after_capital_requirement=100.0,
        )
        assert adj.adjustment_type == kind
