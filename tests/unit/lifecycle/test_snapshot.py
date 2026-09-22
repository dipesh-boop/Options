"""Part 17: the immutable `LifecycleDecisionSnapshot`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.snapshot import LifecycleDecisionSnapshot, build_snapshot
from src.lifecycle.state import PositionLifecycleState as S
from src.lifecycle.triggers import TriggerFinding
from src.strategies.base import StrategyKind

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _resolved() -> ResolvedAction:
    findings = [TriggerFinding(trigger_name="profit_target_pct", category="profit_target", target_state=S.PROFIT_TARGET_REACHED, mandatory=False, reason="60% captured")]
    return ResolvedAction(
        category="profit_target", target_state=S.PROFIT_TARGET_REACHED, mandatory=False, reason="60% captured",
        winning_trigger_names=("profit_target_pct",), all_findings=tuple(findings),
    )


class TestBuildSnapshot:
    def test_records_every_candidate_action(self):
        resolved = _resolved()
        snap = build_snapshot(
            trade_id="T1", wheel_id=None, timestamp=T0, strategy=StrategyKind.PUT_CREDIT_SPREAD,
            management_policy_name="PUT_CREDIT_SPREAD_STANDARD", current_state=S.PROFIT_TARGET_REACHED,
            underlying_price=100.0, mfe=60.0, mae=10.0, unrealized_pnl=60.0, realized_pnl=0.0,
            findings=list(resolved.all_findings), resolved=resolved, risk_status="ok", data_is_fresh=True,
        )
        assert len(snap.candidate_actions) == 1
        assert snap.candidate_actions[0].trigger_name == "profit_target_pct"
        assert snap.deterministic_action_category == "profit_target"
        assert snap.deterministic_action_target_state == S.PROFIT_TARGET_REACHED
        assert snap.reason_codes == ("profit_target_pct",)

    def test_snapshot_is_frozen(self):
        resolved = _resolved()
        snap = build_snapshot(
            trade_id="T1", wheel_id=None, timestamp=T0, strategy=StrategyKind.PUT_CREDIT_SPREAD,
            management_policy_name="X", current_state=S.ACTIVE, underlying_price=None, mfe=0.0, mae=0.0,
            unrealized_pnl=0.0, realized_pnl=0.0, findings=[], resolved=resolved, risk_status="ok", data_is_fresh=True,
        )
        with pytest.raises(Exception):
            snap.unrealized_pnl = 999.0  # type: ignore[misc]

    def test_naive_timestamp_rejected(self):
        with pytest.raises(Exception):
            LifecycleDecisionSnapshot(
                trade_id="T1", timestamp=datetime(2026, 1, 1), strategy=StrategyKind.PUT_CREDIT_SPREAD,
                management_policy="X", current_state=S.ACTIVE, mfe=0.0, mae=0.0, unrealized_pnl=0.0, realized_pnl=0.0,
                deterministic_action_category="hold", deterministic_action_target_state=S.ACTIVE,
                deterministic_action_mandatory=False, deterministic_action_reason="x", risk_status="ok", data_is_fresh=True,
            )

    def test_extra_field_rejected(self):
        with pytest.raises(Exception):
            LifecycleDecisionSnapshot(
                trade_id="T1", timestamp=T0, strategy=StrategyKind.PUT_CREDIT_SPREAD, management_policy="X",
                current_state=S.ACTIVE, mfe=0.0, mae=0.0, unrealized_pnl=0.0, realized_pnl=0.0,
                deterministic_action_category="hold", deterministic_action_target_state=S.ACTIVE,
                deterministic_action_mandatory=False, deterministic_action_reason="x", risk_status="ok",
                data_is_fresh=True, not_a_real_field=123,
            )
