"""Tests for stages 1-2: verify market-data feeds, verify data
freshness."""
from __future__ import annotations

from datetime import timedelta

from src.data.provider import FreshnessStatus
from src.workflows.feed_health import verify_data_freshness, verify_market_data_feeds
from tests.unit.workflows.conftest import NOW, default_pcs_chain


class TestVerifyMarketDataFeeds:
    def test_successful_fetch_is_healthy(self):
        report = verify_market_data_feeds({"XYZ": default_pcs_chain()})
        assert report.all_healthy is True
        assert report.unhealthy_symbols == ()

    def test_exception_is_unhealthy_and_named(self):
        report = verify_market_data_feeds({"XYZ": RuntimeError("timeout")})
        assert report.all_healthy is False
        assert report.unhealthy_symbols == ("XYZ",)
        assert "timeout" in report.results[0].detail

    def test_mixed_results(self):
        report = verify_market_data_feeds({"AAA": RuntimeError("boom"), "BBB": default_pcs_chain("BBB")})
        assert set(report.unhealthy_symbols) == {"AAA"}
        assert report.all_healthy is False


class TestVerifyDataFreshness:
    def test_fresh_chain(self):
        report = verify_data_freshness({"XYZ": default_pcs_chain()}, NOW)
        assert report.all_fresh is True
        assert report.stale_symbols == ()

    def test_stale_chain(self):
        old_chain = default_pcs_chain()
        report = verify_data_freshness({"XYZ": old_chain}, NOW + timedelta(minutes=30))
        assert report.all_fresh is False
        assert report.stale_symbols == ("XYZ",)
        assert report.results[0].status == FreshnessStatus.STALE

    def test_custom_max_age(self):
        chain = default_pcs_chain()
        report = verify_data_freshness({"XYZ": chain}, NOW + timedelta(minutes=5), max_age=timedelta(minutes=1))
        assert report.all_fresh is False

    def test_age_seconds_reported(self):
        chain = default_pcs_chain()
        report = verify_data_freshness({"XYZ": chain}, NOW + timedelta(minutes=2))
        assert report.results[0].age_seconds == 120.0

    def test_empty_input_is_vacuously_fresh(self):
        report = verify_data_freshness({}, NOW)
        assert report.all_fresh is True
