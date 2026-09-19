"""Tests for grouping backtest trades into per-dimension performance
buckets — all 12 dimensions Step 14 names."""
from __future__ import annotations

import pytest

from src.research.performance_breakdown import (
    ALL_ANALYSIS_DIMENSIONS,
    ResearchTradeObservation,
    breakdown_all_dimensions,
    breakdown_by,
)
from tests.unit.research.conftest import make_context, make_observation, make_trade


class TestAllTwelveDimensionsSupported:
    def test_all_dimensions_are_the_exact_spec_list(self):
        assert set(ALL_ANALYSIS_DIMENSIONS) == {
            "strategy", "delta", "dte", "iv_percentile", "market_regime", "underlying",
            "sector", "entry_day", "entry_time", "holding_period", "profit_target", "management_dte",
        }

    def test_breakdown_all_dimensions_covers_every_one(self):
        obs = [make_observation(50.0, day_offset=0)]
        reports = breakdown_all_dimensions(obs)
        assert {r.dimension for r in reports} == set(ALL_ANALYSIS_DIMENSIONS)

    def test_unknown_dimension_rejected(self):
        with pytest.raises(ValueError):
            breakdown_by([make_observation()], "not_a_real_dimension")  # type: ignore[arg-type]


class TestBucketStatsCorrectness:
    def test_win_rate_and_pnl_stats(self):
        obs = [make_observation(100.0, day_offset=0), make_observation(-40.0, day_offset=1), make_observation(60.0, day_offset=2)]
        report = breakdown_by(obs, "strategy")
        assert report.total_trades == 3
        assert len(report.buckets) == 1
        bucket = report.buckets[0]
        assert bucket.trade_count == 3
        assert bucket.win_rate == pytest.approx(2 / 3)
        assert bucket.total_pnl == pytest.approx(120.0)
        assert bucket.average_pnl == pytest.approx(40.0)
        assert bucket.best_pnl == pytest.approx(100.0)
        assert bucket.worst_pnl == pytest.approx(-40.0)

    def test_buckets_sorted_by_bucket_name(self):
        obs = [make_observation(entry_dte=40, day_offset=0), make_observation(entry_dte=5, day_offset=1)]
        report = breakdown_by(obs, "dte")
        assert [b.bucket for b in report.buckets] == sorted(b.bucket for b in report.buckets)


class TestDeltaBucketing:
    @pytest.mark.parametrize("delta,expected", [(-0.05, "0-10"), (-0.12, "10-15"), (-0.18, "15-20"), (-0.22, "20-25"), (-0.28, "25-30"), (-0.35, "30-40"), (-0.50, "40+")])
    def test_delta_buckets(self, delta, expected):
        report = breakdown_by([make_observation(entry_delta=delta)], "delta")
        assert report.buckets[0].bucket == expected

    def test_delta_bucketing_uses_magnitude_not_sign(self):
        neg = breakdown_by([make_observation(entry_delta=-0.18)], "delta")
        pos = breakdown_by([make_observation(entry_delta=0.18)], "delta")
        assert neg.buckets[0].bucket == pos.buckets[0].bucket == "15-20"


class TestDteBucketing:
    @pytest.mark.parametrize("dte,expected", [(3, "0-7"), (10, "7-14"), (18, "14-21"), (25, "21-30"), (35, "30-45"), (60, "45+")])
    def test_dte_buckets(self, dte, expected):
        report = breakdown_by([make_observation(entry_dte=dte)], "dte")
        assert report.buckets[0].bucket == expected


class TestIvPercentileBucketing:
    @pytest.mark.parametrize("pct,expected", [(10, "0-25"), (40, "25-50"), (60, "50-75"), (90, "75-100")])
    def test_iv_percentile_buckets(self, pct, expected):
        report = breakdown_by([make_observation(iv_percentile=pct)], "iv_percentile")
        assert report.buckets[0].bucket == expected


class TestHoldingPeriodBucketing:
    @pytest.mark.parametrize("days,expected", [(3, "0-7d"), (10, "7-14d"), (20, "14-30d"), (35, "30-45d"), (60, "45d+")])
    def test_holding_period_buckets(self, days, expected):
        obs = ResearchTradeObservation(trade=make_trade(50.0, day_offset=0, holding_days=days), context=make_context())
        report = breakdown_by([obs], "holding_period")
        assert report.buckets[0].bucket == expected


class TestUnderlyingAndSectorAndRegimeAndEntryDay:
    def test_underlying_buckets_by_ticker(self):
        obs = [
            ResearchTradeObservation(trade=make_trade(day_offset=0, ticker="AAA"), context=make_context()),
            ResearchTradeObservation(trade=make_trade(day_offset=1, ticker="BBB"), context=make_context()),
        ]
        report = breakdown_by(obs, "underlying")
        assert {b.bucket for b in report.buckets} == {"AAA", "BBB"}

    def test_sector_and_regime_pass_through_directly(self):
        report_sector = breakdown_by([make_observation(sector="tech")], "sector")
        assert report_sector.buckets[0].bucket == "tech"
        report_regime = breakdown_by([make_observation(market_regime="high_iv")], "market_regime")
        assert report_regime.buckets[0].bucket == "high_iv"

    def test_entry_day_derived_from_opened_at(self):
        # 2024-01-01 was a Monday
        report = breakdown_by([make_observation(day_offset=0)], "entry_day")
        assert report.buckets[0].bucket == "Monday"


class TestEntryTimeHonestyAboutMissingData:
    def test_entry_time_buckets_as_unspecified_when_not_supplied(self):
        report = breakdown_by([make_observation()], "entry_time")
        assert report.buckets[0].bucket == "unspecified"

    def test_entry_time_uses_supplied_value_when_present(self):
        obs = make_observation()
        from dataclasses import replace
        obs = type(obs)(trade=obs.trade, context=replace(obs.context, entry_time_of_day="09:35"))
        report = breakdown_by([obs], "entry_time")
        assert report.buckets[0].bucket == "09:35"


class TestProfitTargetAndManagementDte:
    def test_profit_target_bucketed_as_percentage(self):
        report = breakdown_by([make_observation(profit_target_pct=0.5)], "profit_target")
        assert report.buckets[0].bucket == "50%"

    def test_management_dte_bucketed_as_string(self):
        report = breakdown_by([make_observation(management_dte=21)], "management_dte")
        assert report.buckets[0].bucket == "21"
