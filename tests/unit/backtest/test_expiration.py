"""Tests for DTE bookkeeping and management-rule triggers."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.expiration import days_to_expiration, is_expiring_today, management_dte_reached, profit_target_reached
from src.backtest.simulator import BacktestPosition
from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType
from tests.unit.backtest.conftest import cash_secured_put_legs


def _position(**overrides) -> BacktestPosition:
    defaults = dict(
        position_id="bt-1",
        ticker="XYZ",
        strategy=StrategyType.CASH_SECURED_PUT,
        legs=cash_secured_put_legs(),
        contracts=1,
        expiration=date(2024, 2, 16),
        opened_at=date(2024, 1, 2),
        management_dte=7,
        profit_target_pct=0.5,
        realistic_entry_credit_total=200.0,
        theoretical_entry_credit_total=200.0,
        capital_at_risk=9500.0,
    )
    defaults.update(overrides)
    return BacktestPosition(**defaults)


class TestDaysToExpiration:
    def test_positive_when_expiration_in_future(self):
        pos = _position(expiration=date(2024, 2, 16))
        assert days_to_expiration(pos, date(2024, 2, 1)) == 15

    def test_zero_on_expiration_day(self):
        pos = _position(expiration=date(2024, 2, 16))
        assert days_to_expiration(pos, date(2024, 2, 16)) == 0


class TestIsExpiringToday:
    def test_true_on_exact_expiration_date(self):
        pos = _position(expiration=date(2024, 2, 16))
        assert is_expiring_today(pos, date(2024, 2, 16)) is True

    def test_false_before_expiration(self):
        pos = _position(expiration=date(2024, 2, 16))
        assert is_expiring_today(pos, date(2024, 2, 15)) is False

    def test_false_after_expiration_defensively(self):
        pos = _position(expiration=date(2024, 2, 16))
        assert is_expiring_today(pos, date(2024, 2, 17)) is False


class TestManagementDteReached:
    def test_false_when_far_from_management_dte(self):
        pos = _position(expiration=date(2024, 2, 16), management_dte=7)
        assert management_dte_reached(pos, date(2024, 1, 2)) is False

    def test_true_exactly_at_management_dte(self):
        pos = _position(expiration=date(2024, 2, 16), management_dte=7)
        assert management_dte_reached(pos, date(2024, 2, 9)) is True

    def test_true_past_management_dte(self):
        pos = _position(expiration=date(2024, 2, 16), management_dte=7)
        assert management_dte_reached(pos, date(2024, 2, 14)) is True


class TestProfitTargetReached:
    def test_false_when_max_profit_is_non_positive(self):
        pos = _position(profit_target_pct=0.5)
        assert profit_target_reached(pos, current_realistic_value_to_close=0.0, max_profit_per_contract=0.0) is False

    def test_false_below_target_fraction(self):
        pos = _position(profit_target_pct=0.5)
        # cost to close 150 against max profit 200 -> only 25% realized
        assert profit_target_reached(pos, current_realistic_value_to_close=150.0, max_profit_per_contract=200.0) is False

    def test_true_exactly_at_target_fraction(self):
        pos = _position(profit_target_pct=0.5)
        # cost to close 100 against max profit 200 -> exactly 50% realized
        assert profit_target_reached(pos, current_realistic_value_to_close=100.0, max_profit_per_contract=200.0) is True

    def test_true_above_target_fraction(self):
        pos = _position(profit_target_pct=0.5)
        assert profit_target_reached(pos, current_realistic_value_to_close=10.0, max_profit_per_contract=200.0) is True

    def test_true_when_position_is_worthless_full_max_profit(self):
        pos = _position(profit_target_pct=0.5)
        assert profit_target_reached(pos, current_realistic_value_to_close=0.0, max_profit_per_contract=200.0) is True
