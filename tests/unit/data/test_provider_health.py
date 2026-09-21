"""Tests for `src.data.provider_health.check_provider_health`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.data.factory import DataProviderSelection
from src.data.provider_health import DataConnectionStatus, check_provider_health

# A known Tuesday during regular NYSE hours.
MARKET_OPEN_NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
# A known Saturday.
MARKET_CLOSED_NOW = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)


class TestMockProviderHealth:
    @pytest.mark.asyncio
    async def test_mock_reports_mock_data_status(self):
        report = await check_provider_health(now=MARKET_OPEN_NOW, selection=DataProviderSelection(data_provider="mock"))
        assert report.connection_status == DataConnectionStatus.MOCK_DATA
        assert report.provider_selected == "mock"
        assert report.equity_data_available is True
        assert report.options_data_available is True
        assert report.authenticated is None  # not applicable to mock
        assert report.opra_entitled is None
        assert report.errors == ()

    @pytest.mark.asyncio
    async def test_market_open_flag_reflects_market_calendar(self):
        open_report = await check_provider_health(now=MARKET_OPEN_NOW, selection=DataProviderSelection(data_provider="mock"))
        assert open_report.market_open is True

        closed_report = await check_provider_health(now=MARKET_CLOSED_NOW, selection=DataProviderSelection(data_provider="mock"))
        assert closed_report.market_open is False
        assert "closed" in closed_report.market_status_detail


class TestUnknownProviderHealth:
    @pytest.mark.asyncio
    async def test_unknown_provider_name_reports_unavailable_not_a_crash(self):
        report = await check_provider_health(now=MARKET_OPEN_NOW, selection=DataProviderSelection(data_provider="robinhood"))
        assert report.connection_status == DataConnectionStatus.REAL_DATA_UNAVAILABLE
        assert report.equity_data_available is False
        assert report.options_data_available is False
        assert len(report.errors) == 1
        assert "configuration error" in report.errors[0]


class TestAlpacaProviderHealthWithoutCredentials:
    @pytest.mark.asyncio
    async def test_alpaca_without_credentials_reports_unavailable_never_crashes_the_caller(self):
        report = await check_provider_health(now=MARKET_OPEN_NOW, selection=DataProviderSelection(data_provider="alpaca"))
        assert report.connection_status == DataConnectionStatus.REAL_DATA_UNAVAILABLE
        assert report.authenticated is False
        assert report.equity_data_available is False
        assert report.options_data_available is False
        assert len(report.errors) >= 1
