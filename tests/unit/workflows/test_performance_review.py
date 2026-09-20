"""Tests for src.workflows.performance_review: PORTFOLIO PERFORMANCE,
RISK, TRADE STATISTICS, and BREAK DOWN PERFORMANCE, all built on
already-tested src.backtest.metrics/benchmark and
src.research.performance_breakdown."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.simulator import TradeRecord
from src.data.historical import HistoricalBar
from src.llm.schemas import StrategyType
from src.research.performance_breakdown import ResearchTradeObservation, TradeContext
from src.workflows.performance_review import (
    WEEKLY_REVIEW_DIMENSIONS,
    build_breakdowns,
    build_portfolio_performance,
    build_risk_section,
    build_trade_statistics,
)


def _bar(bar_date: date, close: float) -> HistoricalBar:
    return HistoricalBar(symbol="SPY", bar_date=bar_date, open=close, high=close, low=close, close=close, volume=1_000_000, source="mock")


def _trade(pnl: float = 50.0, **overrides) -> TradeRecord:
    base = dict(
        position_id="p", ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD, contracts=1,
        opened_at=date(2026, 9, 1), closed_at=date(2026, 9, 10), close_reason="profit_target", capital_at_risk=500.0,
        entry_spread_pct=0.05, realistic_entry_credit=100.0, realistic_exit_debit=-(100.0 - pnl),
        theoretical_entry_credit=100.0, theoretical_exit_debit=-(100.0 - pnl), commission_paid=1.3,
        realistic_pnl=pnl, theoretical_pnl=pnl,
    )
    base.update(overrides)
    return TradeRecord(**base)


class TestBuildPortfolioPerformance:
    def test_computes_period_returns_from_curve_anchors(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 9, 1), 105_000.0), (date(2026, 9, 13), 108_000.0), (date(2026, 9, 20), 109_000.0)]
        perf = build_portfolio_performance(curve, week_start=date(2026, 9, 13), month_start=date(2026, 9, 1), year_start=date(2026, 1, 1), risk_free_annual_rate=0.04)
        assert perf.starting_nav == 100_000.0
        assert perf.ending_nav == 109_000.0
        assert perf.weekly_return == pytest.approx(109_000.0 / 108_000.0 - 1.0)
        assert perf.mtd_return == pytest.approx(109_000.0 / 105_000.0 - 1.0)
        assert perf.ytd_return == pytest.approx(109_000.0 / 100_000.0 - 1.0)
        assert perf.since_inception_return == pytest.approx(0.09)

    def test_returns_none_when_no_anchor_point_exists_before_boundary(self):
        curve = [(date(2026, 9, 15), 100_000.0), (date(2026, 9, 20), 101_000.0)]
        perf = build_portfolio_performance(curve, week_start=date(2020, 1, 1), month_start=date(2020, 1, 1), year_start=date(2020, 1, 1), risk_free_annual_rate=0.04)
        # every boundary is before the curve even begins -- no anchor point qualifies, so each
        # period return is honestly None rather than silently computed against the wrong point
        assert perf.weekly_return is None
        assert perf.mtd_return is None
        assert perf.ytd_return is None
        assert perf.since_inception_return == pytest.approx(0.01)

    def test_requires_at_least_two_points(self):
        with pytest.raises(ValueError):
            build_portfolio_performance([(date(2026, 1, 1), 100_000.0)], week_start=date(2026, 1, 1), month_start=date(2026, 1, 1), year_start=date(2026, 1, 1), risk_free_annual_rate=0.04)

    def test_benchmark_included_when_spy_bars_supplied(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 9, 20), 110_000.0)]
        spy_bars = [_bar(date(2026, 1, 1), 400.0), _bar(date(2026, 9, 20), 420.0)]
        perf = build_portfolio_performance(curve, week_start=date(2026, 9, 13), month_start=date(2026, 9, 1), year_start=date(2026, 1, 1), risk_free_annual_rate=0.04, spy_bars=spy_bars)
        assert perf.benchmark is not None
        assert perf.benchmark.spy_total_return == pytest.approx(0.05)

    def test_no_benchmark_when_spy_bars_omitted(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 9, 20), 110_000.0)]
        perf = build_portfolio_performance(curve, week_start=date(2026, 9, 13), month_start=date(2026, 9, 1), year_start=date(2026, 1, 1), risk_free_annual_rate=0.04)
        assert perf.benchmark is None


class TestBuildRiskSection:
    def test_reuses_backtest_metrics_formulas(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 6, 1), 90_000.0), (date(2026, 9, 20), 110_000.0)]
        risk = build_risk_section(curve, risk_free_annual_rate=0.04, current_drawdown_pct=0.0, cash=50_000.0, capital_deployed_pct=0.5, sector_exposure={"TECH": 0.2}, underlying_concentration={"XYZ": 0.1})
        assert risk.max_drawdown == pytest.approx(0.10)
        assert risk.cash == 50_000.0
        assert risk.sector_exposure == {"TECH": 0.2}

    def test_untracked_greeks_default_to_none(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 9, 20), 105_000.0)]
        risk = build_risk_section(curve, risk_free_annual_rate=0.04, current_drawdown_pct=0.0, cash=1.0, capital_deployed_pct=0.0, sector_exposure={}, underlying_concentration={})
        assert risk.net_delta is None
        assert risk.net_theta is None
        assert risk.net_vega is None

    def test_supplied_greeks_pass_through(self):
        curve = [(date(2026, 1, 1), 100_000.0), (date(2026, 9, 20), 105_000.0)]
        risk = build_risk_section(curve, risk_free_annual_rate=0.04, current_drawdown_pct=0.0, cash=1.0, capital_deployed_pct=0.0, sector_exposure={}, underlying_concentration={}, net_delta=-10.0)
        assert risk.net_delta == -10.0


class TestBuildTradeStatistics:
    def test_matches_backtest_metrics_trade_statistics(self):
        trades = [_trade(100.0), _trade(-30.0)]
        stats = build_trade_statistics(trades)
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["expectancy"] == pytest.approx(35.0)

    def test_empty_trades(self):
        stats = build_trade_statistics([])
        assert stats["win_rate"] == 0.0


class TestBuildBreakdowns:
    def test_covers_exactly_the_eight_named_dimensions(self):
        assert WEEKLY_REVIEW_DIMENSIONS == ("strategy", "market_regime", "delta", "dte", "iv_percentile", "underlying", "sector", "holding_period")

    def test_returns_one_report_per_dimension(self):
        trade = _trade()
        obs = [ResearchTradeObservation(trade=trade, context=TradeContext(entry_delta=-0.2, entry_dte=25, iv_percentile=70.0, market_regime="normal", sector="TECH", profit_target_pct=0.5, management_dte=7))]
        reports = build_breakdowns(obs)
        assert len(reports) == 8
        assert {r.dimension for r in reports} == set(WEEKLY_REVIEW_DIMENSIONS)

    def test_empty_observations_produce_empty_buckets(self):
        reports = build_breakdowns([])
        assert all(r.buckets == () for r in reports)
