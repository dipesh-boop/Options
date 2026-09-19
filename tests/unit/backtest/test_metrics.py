"""Tests for `src.backtest.metrics` -- each formula hand-checked against
a small, exact synthetic example, per this package's own "independently
verifiable" standard (see the module's own docstring)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.backtest.metrics import (
    average_holding_period_days,
    cagr,
    calmar_ratio,
    capital_utilization,
    compute_metrics,
    historical_cvar,
    historical_var,
    longest_drawdown_days,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    trade_statistics,
    worst_month,
    worst_year,
)
from src.backtest.simulator import TradeRecord
from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType


def _curve(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    return points


def _trade(**overrides) -> TradeRecord:
    defaults = dict(
        position_id="bt-1",
        ticker="XYZ",
        strategy=StrategyType.CASH_SECURED_PUT,
        contracts=1,
        opened_at=date(2024, 1, 2),
        closed_at=date(2024, 1, 16),
        close_reason="profit_target",
        capital_at_risk=9500.0,
        entry_spread_pct=0.05,
        realistic_entry_credit=200.0,
        realistic_exit_debit=-100.0,
        theoretical_entry_credit=200.0,
        theoretical_exit_debit=-100.0,
        commission_paid=1.30,
        realistic_pnl=98.70,
        theoretical_pnl=100.0,
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


class TestCagr:
    def test_exact_ten_percent_per_year_over_two_years(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2024, 1, 1), 121.0)])
        assert cagr(curve) == pytest.approx(0.10, abs=1e-3)

    def test_zero_for_fewer_than_two_points(self):
        assert cagr([(date(2022, 1, 1), 100.0)]) == 0.0
        assert cagr([]) == 0.0

    def test_negative_cagr_for_a_loss(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2023, 1, 1), 90.0)])
        assert cagr(curve) < 0.0

    def test_non_positive_starting_equity_raises(self):
        curve = _curve([(date(2022, 1, 1), 0.0), (date(2023, 1, 1), 100.0)])
        with pytest.raises(ValueError):
            cagr(curve)


class TestMaxDrawdown:
    def test_exact_25_percent_drawdown(self):
        curve = _curve([
            (date(2022, 1, 1), 100.0),
            (date(2022, 2, 1), 120.0),
            (date(2022, 3, 1), 90.0),
            (date(2022, 4, 1), 130.0),
        ])
        assert max_drawdown(curve) == pytest.approx(0.25)

    def test_zero_for_monotonically_increasing_curve(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2022, 2, 1), 110.0), (date(2022, 3, 1), 120.0)])
        assert max_drawdown(curve) == 0.0

    def test_empty_curve_is_zero(self):
        assert max_drawdown([]) == 0.0


class TestLongestDrawdownDays:
    def test_measures_duration_not_depth(self):
        curve = _curve([
            (date(2022, 1, 1), 100.0),
            (date(2022, 1, 31), 90.0),   # 30 days into a drawdown
            (date(2022, 3, 1), 95.0),    # still below peak -- 59 days since the peak
            (date(2022, 3, 2), 101.0),   # new high -- drawdown ends; duration is measured up to the last still-underwater day
        ])
        assert longest_drawdown_days(curve) == 59

    def test_zero_when_curve_only_makes_new_highs(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2022, 2, 1), 110.0)])
        assert longest_drawdown_days(curve) == 0


class TestCalmarRatio:
    def test_ratio_of_cagr_to_max_drawdown(self):
        curve = _curve([
            (date(2022, 1, 1), 100.0),
            (date(2022, 7, 1), 80.0),
            (date(2024, 1, 1), 121.0),
        ])
        expected = cagr(curve) / max_drawdown(curve)
        assert calmar_ratio(curve) == pytest.approx(expected)

    def test_zero_when_no_drawdown_occurred(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2023, 1, 1), 110.0)])
        assert calmar_ratio(curve) == 0.0


class TestSharpeAndSortinoEdgeCases:
    def test_sharpe_zero_when_returns_have_no_variance(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2022, 2, 1), 110.0), (date(2022, 3, 1), 121.0)])
        assert sharpe_ratio(curve, risk_free_annual_rate=0.0) == 0.0

    def test_sortino_zero_when_there_is_no_downside_deviation(self):
        """An all-gains series (no period ever below the risk-free hurdle
        downward) has zero downside deviation -- sortino is explicitly
        0.0 rather than infinite, a documented, deterministic edge case."""
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2022, 2, 1), 110.0), (date(2022, 3, 1), 130.0)])
        assert sortino_ratio(curve, risk_free_annual_rate=0.0) == 0.0

    def test_sharpe_and_sortino_zero_for_fewer_than_two_returns(self):
        curve = _curve([(date(2022, 1, 1), 100.0), (date(2022, 2, 1), 110.0)])
        assert sharpe_ratio(curve, 0.0) == 0.0
        assert sortino_ratio(curve, 0.0) == 0.0


class TestWorstMonthWorstYear:
    def test_worst_month_picks_the_single_worst_calendar_month_return(self):
        curve = _curve([
            (date(2022, 1, 1), 100.0),
            (date(2022, 1, 31), 105.0),  # Jan: +5%
            (date(2022, 2, 1), 105.0),
            (date(2022, 2, 28), 90.0),   # Feb: -14.28...%
        ])
        assert worst_month(curve) == pytest.approx(90.0 / 105.0 - 1.0)

    def test_worst_year_picks_the_single_worst_calendar_year_return(self):
        curve = _curve([
            (date(2022, 1, 1), 100.0),
            (date(2022, 12, 31), 110.0),  # 2022: +10%
            (date(2023, 1, 1), 110.0),
            (date(2023, 12, 31), 88.0),   # 2023: -20%
        ])
        assert worst_year(curve) == pytest.approx(-0.20)

    def test_empty_curve_returns_zero(self):
        assert worst_month([]) == 0.0
        assert worst_year([]) == 0.0


class TestHistoricalVarCvar:
    def test_var_and_cvar_on_a_clean_five_point_series(self):
        # returns: -10%, -5%, 0%, +5%, +10% via a simple compounding curve
        base = date(2022, 1, 1)
        equities = [100.0]
        for r in (-0.10, -0.05, 0.00, 0.05, 0.10):
            equities.append(equities[-1] * (1 + r))
        curve = [(base + timedelta(days=i), e) for i, e in enumerate(equities)]
        # confidence=0.95, n=5 -> index = floor(0.25) - 1 = -1 -> clamped to 0 -> worst return
        assert historical_var(curve, confidence=0.95) == pytest.approx(0.10)
        assert historical_cvar(curve, confidence=0.95) == pytest.approx(0.10)

    def test_cvar_averages_the_tail_beyond_var(self):
        base = date(2022, 1, 1)
        # 40 returns: 0%, -1%, -2%, ..., -39%, applied sequentially
        rs = [-0.01 * i for i in range(40)]
        equities = [1000.0]
        for r in rs:
            equities.append(equities[-1] * (1 + r))
        curve = [(base + timedelta(days=i), e) for i, e in enumerate(equities)]
        # confidence=0.95, n=40 -> cutoff = floor(0.05*40) = 2 -> two worst returns averaged for cvar
        var95 = historical_var(curve, confidence=0.95)
        cvar95 = historical_cvar(curve, confidence=0.95)
        assert cvar95 >= var95  # CVaR is always at least as severe as VaR
        assert cvar95 == pytest.approx(0.385, abs=1e-6)
        assert var95 == pytest.approx(0.38, abs=1e-6)

    def test_empty_curve_returns_zero(self):
        assert historical_var([]) == 0.0
        assert historical_cvar([]) == 0.0


class TestTradeStatistics:
    def test_win_rate_average_winner_loser_profit_factor_expectancy(self):
        trades = [
            _trade(realistic_pnl=100.0),
            _trade(realistic_pnl=200.0),
            _trade(realistic_pnl=-50.0),
        ]
        stats = trade_statistics(trades)
        assert stats["win_rate"] == pytest.approx(2 / 3)
        assert stats["average_winner"] == pytest.approx(150.0)
        assert stats["average_loser"] == pytest.approx(50.0)
        assert stats["profit_factor"] == pytest.approx(300.0 / 50.0)
        assert stats["expectancy"] == pytest.approx((100.0 + 200.0 - 50.0) / 3)

    def test_no_trades_returns_zeroed_stats(self):
        stats = trade_statistics([])
        assert stats == dict(win_rate=0.0, average_winner=0.0, average_loser=0.0, profit_factor=0.0, expectancy=0.0)

    def test_profit_factor_infinite_when_no_losers_but_gains_exist(self):
        stats = trade_statistics([_trade(realistic_pnl=100.0)])
        assert stats["profit_factor"] == float("inf")


class TestCapitalUtilization:
    def test_fully_deployed_the_whole_period(self):
        curve = _curve([(date(2024, 1, 1), 100_000.0), (date(2024, 1, 11), 100_000.0)])
        trades = [_trade(opened_at=date(2024, 1, 1), closed_at=date(2024, 1, 11), capital_at_risk=100_000.0)]
        assert capital_utilization(trades, curve, initial_cash=100_000.0) == pytest.approx(1.0)

    def test_never_in_a_position_is_zero(self):
        curve = _curve([(date(2024, 1, 1), 100_000.0), (date(2024, 1, 11), 100_000.0)])
        assert capital_utilization([], curve, initial_cash=100_000.0) == 0.0

    def test_half_the_period_half_deployed(self):
        curve = _curve([(date(2024, 1, 1), 100_000.0), (date(2024, 1, 21), 100_000.0)])  # 20 days
        trades = [_trade(opened_at=date(2024, 1, 1), closed_at=date(2024, 1, 11), capital_at_risk=100_000.0)]  # 10 days
        assert capital_utilization(trades, curve, initial_cash=100_000.0) == pytest.approx(0.5)


class TestAverageHoldingPeriodDays:
    def test_averages_across_trades(self):
        trades = [
            _trade(opened_at=date(2024, 1, 1), closed_at=date(2024, 1, 11)),  # 10 days
            _trade(opened_at=date(2024, 1, 1), closed_at=date(2024, 1, 21)),  # 20 days
        ]
        assert average_holding_period_days(trades) == pytest.approx(15.0)

    def test_empty_trades_is_zero(self):
        assert average_holding_period_days([]) == 0.0


class TestComputeMetricsWiring:
    def test_returns_a_fully_populated_metrics_object(self):
        curve = _curve([(date(2022, 1, 1), 100_000.0), (date(2022, 6, 1), 105_000.0), (date(2023, 1, 1), 112_000.0)])
        trades = [_trade(realistic_pnl=50.0), _trade(realistic_pnl=-20.0)]
        metrics = compute_metrics(curve, trades, initial_cash=100_000.0, risk_free_annual_rate=0.04)
        assert metrics.trade_count == 2
        assert metrics.win_rate == pytest.approx(0.5)
        assert metrics.max_drawdown >= 0.0
