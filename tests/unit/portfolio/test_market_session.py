"""PAPER_TRADING_V1.5.1, Step 2: deterministic tests for
`src.portfolio.market_session.evaluate_validation_cycle_eligibility` --
the new-position daily validation cycle's market-hours safety gate.

Every test uses an explicit, injected, timezone-aware `datetime` --
never the actual current clock (`datetime.now()` is never called
anywhere in this file). Reference dates below are taken directly from
`tests/unit/data/test_market_calendar.py`'s own already-verified
calendar facts (2026-09-21 is a normal Monday trading day, 2026-09-26/
27 is a weekend, 2026-01-01 is a holiday, 2026-11-27 is an early-close
trading day), so this file never re-derives the NYSE calendar itself --
it only exercises the buffer/decision layer on top of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.data import market_calendar
from src.portfolio.market_session import (
    MarketSessionState,
    evaluate_validation_cycle_eligibility,
)

ET = ZoneInfo("America/New_York")

# A normal Monday trading day (confirmed by test_market_calendar.py).
MONDAY = 2026, 9, 21


def _et(hour: int, minute: int, *, day=MONDAY) -> datetime:
    year, month, dom = day
    return datetime(year, month, dom, hour, minute, tzinfo=ET)


class TestNumberedScenarios:
    """The 25-scenario list's market-session-specific items (1-12),
    numbered to match the task specification exactly."""

    def test_1_regular_trading_day_before_market_open_is_blocked(self):
        result = evaluate_validation_cycle_eligibility(_et(9, 0))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.PRE_MARKET

    def test_2_exactly_at_exchange_open_respects_the_open_buffer_policy(self):
        # Default policy: 5-minute post-open buffer -- exactly 9:30 is
        # still within it, so the scan is not yet allowed.
        result = evaluate_validation_cycle_eligibility(_et(9, 30))
        assert result.validation_cycle_allowed is False
        assert "buffer" in result.block_reason.lower()

    def test_3_after_the_approved_opening_buffer_is_allowed(self):
        result = evaluate_validation_cycle_eligibility(_et(9, 35))
        assert result.validation_cycle_allowed is True
        assert result.block_reason is None

    def test_4_during_regular_session_is_allowed(self):
        result = evaluate_validation_cycle_eligibility(_et(12, 0))
        assert result.validation_cycle_allowed is True
        assert result.market_session_state is MarketSessionState.REGULAR_MARKET

    def test_5_near_and_after_session_close_respects_the_closing_buffer_policy(self):
        # Default policy: 15-minute pre-close buffer -- 3:50pm is within
        # it (close is 4:00pm), so blocked; 3:40pm is not, so allowed.
        blocked = evaluate_validation_cycle_eligibility(_et(15, 50))
        assert blocked.validation_cycle_allowed is False
        assert "buffer" in blocked.block_reason.lower()

        allowed = evaluate_validation_cycle_eligibility(_et(15, 40))
        assert allowed.validation_cycle_allowed is True

    def test_6_after_market_close_is_blocked(self):
        result = evaluate_validation_cycle_eligibility(_et(16, 30))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.POST_MARKET

    def test_7_saturday_is_blocked(self):
        result = evaluate_validation_cycle_eligibility(_et(12, 0, day=(2026, 9, 26)))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.MARKET_CLOSED
        assert result.is_trading_day is False

    def test_8_sunday_is_blocked(self):
        result = evaluate_validation_cycle_eligibility(_et(12, 0, day=(2026, 9, 27)))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.MARKET_CLOSED
        assert result.is_trading_day is False

    def test_9_us_market_holiday_is_blocked(self):
        # New Year's Day 2026 (Thursday) -- confirmed a holiday by
        # test_market_calendar.py.
        result = evaluate_validation_cycle_eligibility(_et(12, 0, day=(2026, 1, 1)))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.MARKET_CLOSED

    def test_10_early_close_trading_day_respects_the_actual_early_close(self):
        # Day after Thanksgiving 2026-11-27: regular close is 1:00pm ET,
        # not 4:00pm -- confirmed by test_market_calendar.py.
        day = (2026, 11, 27)
        mid_session = evaluate_validation_cycle_eligibility(_et(12, 0, day=day))
        assert mid_session.validation_cycle_allowed is True
        assert mid_session.regular_session_close == datetime(2026, 11, 27, 13, 0, tzinfo=ET)

        within_close_buffer = evaluate_validation_cycle_eligibility(_et(12, 50, day=day))
        assert within_close_buffer.validation_cycle_allowed is False

        after_early_close = evaluate_validation_cycle_eligibility(_et(13, 30, day=day))
        assert after_early_close.validation_cycle_allowed is False
        assert after_early_close.market_session_state is MarketSessionState.POST_MARKET

    def test_11_timezone_conversion_same_instant_utc_and_et_agree(self):
        et_instant = _et(12, 0)
        utc_instant = et_instant.astimezone(timezone.utc)
        result_et = evaluate_validation_cycle_eligibility(et_instant)
        result_utc = evaluate_validation_cycle_eligibility(utc_instant)
        assert result_et.validation_cycle_allowed == result_utc.validation_cycle_allowed
        assert result_et.market_session_state == result_utc.market_session_state
        # datetime equality compares the instant, not the tzinfo object --
        # same instant, deliberately passed in two different timezones.
        assert result_et.as_of == result_utc.as_of
        assert result_et.regular_session_open == result_utc.regular_session_open

    def test_12_calendar_lookup_failure_fails_closed(self, monkeypatch):
        def _boom(_dt):
            raise RuntimeError("simulated calendar failure")

        monkeypatch.setattr(market_calendar, "market_status", _boom)
        result = evaluate_validation_cycle_eligibility(_et(12, 0))
        assert result.validation_cycle_allowed is False
        assert result.market_session_state is MarketSessionState.MARKET_CLOSED
        assert "calendar" in result.block_reason.lower()


class TestNaiveDatetimeRejected:
    def test_naive_datetime_raises_not_silently_fails_closed(self):
        with pytest.raises(market_calendar.NaiveDatetimeError):
            evaluate_validation_cycle_eligibility(datetime(2026, 9, 21, 12, 0))


class TestConfigurableBuffers:
    def test_zero_buffers_allow_exactly_at_open_and_close(self):
        at_open = evaluate_validation_cycle_eligibility(
            _et(9, 30), scan_open_buffer_minutes=0, scan_close_buffer_minutes=0,
        )
        assert at_open.validation_cycle_allowed is True

        just_before_close = evaluate_validation_cycle_eligibility(
            _et(15, 59), scan_open_buffer_minutes=0, scan_close_buffer_minutes=0,
        )
        assert just_before_close.validation_cycle_allowed is True

    def test_wider_buffers_narrow_the_allowed_window(self):
        result = evaluate_validation_cycle_eligibility(
            _et(9, 45), scan_open_buffer_minutes=30, scan_close_buffer_minutes=15,
        )
        assert result.validation_cycle_allowed is False


class TestDisplayFields:
    def test_regular_session_open_close_reported_on_a_trading_day(self):
        result = evaluate_validation_cycle_eligibility(_et(12, 0))
        assert result.regular_session_open == datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        assert result.regular_session_close == datetime(2026, 9, 21, 16, 0, tzinfo=ET)

    def test_regular_session_open_close_are_none_on_a_non_trading_day(self):
        result = evaluate_validation_cycle_eligibility(_et(12, 0, day=(2026, 9, 26)))
        assert result.regular_session_open is None
        assert result.regular_session_close is None

    def test_block_reason_is_none_exactly_when_allowed(self):
        allowed = evaluate_validation_cycle_eligibility(_et(12, 0))
        assert allowed.validation_cycle_allowed is True
        assert allowed.block_reason is None

        blocked = evaluate_validation_cycle_eligibility(_et(9, 0))
        assert blocked.validation_cycle_allowed is False
        assert blocked.block_reason is not None
