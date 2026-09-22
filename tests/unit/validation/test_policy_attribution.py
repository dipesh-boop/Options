"""Part 25: dual-level performance -- STRATEGY alone and STRATEGY +
MANAGEMENT POLICY together, never assuming one policy is superior."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.strategies.base import StrategyKind
from src.validation.policy_attribution import (
    all_strategy_performance,
    all_strategy_policy_performance,
    strategy_level_performance,
    strategy_policy_level_performance,
)
from src.validation.post_trade_analysis import build_closed_position_analysis

D0 = date(2026, 1, 1)


def _pcs_standard_records(n=5):
    return [
        build_closed_position_analysis(
            trade_id=f"PCS-STD-{i}", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, management_policy_name="PUT_CREDIT_SPREAD_STANDARD",
            entry_date=D0 + timedelta(days=i * 10), exit_date=D0 + timedelta(days=i * 10 + 14),
            realized_pnl=(100.0 if i % 4 != 0 else -150.0), capital_at_risk=1000.0, capital_committed=1000.0,
            mfe=120.0, mae=-30.0, commissions=1.3, exit_reason="profit_target_reached",
        )
        for i in range(n)
    ]


def _pcs_hold_records(n=3):
    return [
        build_closed_position_analysis(
            trade_id=f"PCS-HOLD-{i}", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, management_policy_name="PCS_HOLD_TO_EXPIRY",
            entry_date=D0 + timedelta(days=i * 20), exit_date=D0 + timedelta(days=i * 20 + 45),
            realized_pnl=200.0, capital_at_risk=1000.0, capital_committed=1000.0, mfe=200.0, mae=-10.0,
            commissions=1.3, exit_reason="expired_worthless",
        )
        for i in range(n)
    ]


def _csp_assigned_record():
    return build_closed_position_analysis(
        trade_id="CSP-assigned-1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
        entry_date=D0, exit_date=D0 + timedelta(days=30), realized_pnl=-500.0, capital_at_risk=9500.0,
        capital_committed=9500.0, mfe=50.0, mae=-500.0, commissions=0.65, exit_reason="assigned",
    )


@pytest.fixture
def records():
    return _pcs_standard_records() + _pcs_hold_records() + [_csp_assigned_record()]


class TestStrategyLevel:
    def test_groups_across_all_policies_for_that_strategy(self, records):
        g = strategy_level_performance(records, StrategyKind.PUT_CREDIT_SPREAD, starting_capital=100_000.0)
        assert g.trade_count == 8
        assert g.management_policy_name is None

    def test_empty_group_does_not_crash(self, records):
        g = strategy_level_performance(records, StrategyKind.LONG_CALL, starting_capital=100_000.0)
        assert g.trade_count == 0
        assert g.sharpe == 0.0
        assert g.sample_size_warning is not None


class TestStrategyPolicyLevel:
    def test_same_structure_two_policies_have_different_profiles(self, records):
        standard = strategy_policy_level_performance(records, StrategyKind.PUT_CREDIT_SPREAD, "PUT_CREDIT_SPREAD_STANDARD", starting_capital=100_000.0)
        hold = strategy_policy_level_performance(records, StrategyKind.PUT_CREDIT_SPREAD, "PCS_HOLD_TO_EXPIRY", starting_capital=100_000.0)
        assert standard.trade_count == 5
        assert hold.trade_count == 3
        assert standard.win_rate != hold.win_rate
        assert hold.win_rate == 1.0

    def test_group_label_names_both_strategy_and_policy(self, records):
        g = strategy_policy_level_performance(records, StrategyKind.PUT_CREDIT_SPREAD, "PUT_CREDIT_SPREAD_STANDARD", starting_capital=100_000.0)
        assert "put_credit_spread" in g.group_label
        assert "PUT_CREDIT_SPREAD_STANDARD" in g.group_label


class TestAssignmentRate:
    def test_assignment_rate_computed_from_exit_reason(self, records):
        g = strategy_level_performance(records, StrategyKind.CASH_SECURED_PUT, starting_capital=100_000.0)
        assert g.assignment_rate == 1.0

    def test_zero_when_no_assignments(self, records):
        g = strategy_policy_level_performance(records, StrategyKind.PUT_CREDIT_SPREAD, "PCS_HOLD_TO_EXPIRY", starting_capital=100_000.0)
        assert g.assignment_rate == 0.0


class TestSmallSampleWarning:
    def test_below_threshold_carries_a_warning(self, records):
        g = strategy_level_performance(records, StrategyKind.CASH_SECURED_PUT, starting_capital=100_000.0)
        assert g.sample_size_warning is not None
        assert "small sample" in g.sample_size_warning


class TestAllGroupHelpers:
    def test_all_strategy_performance_covers_every_present_strategy(self, records):
        groups = all_strategy_performance(records, starting_capital=100_000.0)
        assert {g.strategy_kind for g in groups} == {StrategyKind.PUT_CREDIT_SPREAD, StrategyKind.CASH_SECURED_PUT}

    def test_all_strategy_policy_performance_covers_every_present_pair(self, records):
        groups = all_strategy_policy_performance(records, starting_capital=100_000.0)
        labels = {g.group_label for g in groups}
        assert labels == {
            "cash_secured_put + CSP_STANDARD",
            "put_credit_spread + PCS_HOLD_TO_EXPIRY",
            "put_credit_spread + PUT_CREDIT_SPREAD_STANDARD",
        }
