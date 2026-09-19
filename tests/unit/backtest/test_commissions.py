"""Tests for the commission schedule/formula."""
from __future__ import annotations

import pytest

from src.backtest.commissions import CommissionSchedule, calculate_commission


class TestCalculateCommission:
    def test_default_schedule_single_leg(self):
        schedule = CommissionSchedule()
        assert calculate_commission(contracts=2, num_legs=1, schedule=schedule) == pytest.approx(1.30)

    def test_default_schedule_multi_leg_charges_per_leg(self):
        schedule = CommissionSchedule(per_contract=0.65)
        # a 2-leg spread, 3 contracts -> 3 * 2 * 0.65
        assert calculate_commission(contracts=3, num_legs=2, schedule=schedule) == pytest.approx(3.90)

    def test_per_leg_base_fee_applied_once_per_leg_not_per_contract(self):
        schedule = CommissionSchedule(per_contract=0.0, per_leg_base=1.00)
        assert calculate_commission(contracts=5, num_legs=2, schedule=schedule) == pytest.approx(2.00)

    def test_zero_contracts_charges_only_per_leg_base(self):
        schedule = CommissionSchedule(per_contract=0.65, per_leg_base=1.00)
        assert calculate_commission(contracts=0, num_legs=2, schedule=schedule) == pytest.approx(2.00)

    def test_negative_contracts_rejected(self):
        with pytest.raises(ValueError):
            calculate_commission(contracts=-1, num_legs=1, schedule=CommissionSchedule())

    def test_zero_legs_rejected(self):
        with pytest.raises(ValueError):
            calculate_commission(contracts=1, num_legs=0, schedule=CommissionSchedule())
