"""Step 22 Part 9: deterministic market-calendar tests. Every date used
below is a fixed, named calendar date -- never `date.today()` or
`datetime.now()` -- so this suite's result never depends on what day it
actually runs."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.data.market_calendar import (
    MarketClosedError,
    NaiveDatetimeError,
    current_session_date,
    is_early_close,
    is_market_open,
    is_trading_day,
    market_status,
    next_market_open,
    next_trading_day,
    regular_close,
    regular_open,
    require_market_open_for_execution,
)

EASTERN = ZoneInfo("America/New_York")


def et(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


class TestOrdinaryWeekdays:
    def test_normal_monday_is_a_trading_day(self):
        # 2026-09-21 is a Monday.
        assert is_trading_day(date(2026, 9, 21)) is True

    def test_normal_friday_is_a_trading_day(self):
        # 2026-09-25 is a Friday.
        assert is_trading_day(date(2026, 9, 25)) is True


class TestWeekends:
    def test_saturday_is_never_a_trading_day(self):
        assert is_trading_day(date(2026, 9, 26)) is False

    def test_sunday_is_never_a_trading_day(self):
        assert is_trading_day(date(2026, 9, 27)) is False


class TestNamedUsMarketHolidays:
    def test_new_years_day_2026_thursday_is_closed(self):
        assert is_trading_day(date(2026, 1, 1)) is False

    def test_new_years_day_2028_observed_shifts_off_the_weekend(self):
        # Jan 1 2028 is a Saturday -- NYSE observes New Year's on... it
        # would already have been observed the prior year end (Dec 31
        #2027, a Friday) per the standard Saturday->Friday shift.
        assert date(2028, 1, 1).weekday() == 5
        assert is_trading_day(date(2027, 12, 31)) is False  # observed Friday
        assert is_trading_day(date(2028, 1, 1)) is False  # the actual Saturday (not a trading day anyway)

    def test_mlk_day_2026_is_closed(self):
        # 3rd Monday in January 2026 = Jan 19.
        assert date(2026, 1, 19).weekday() == 0
        assert is_trading_day(date(2026, 1, 19)) is False

    def test_presidents_day_2026_is_closed(self):
        # 3rd Monday in February 2026 = Feb 16.
        assert is_trading_day(date(2026, 2, 16)) is False

    def test_good_friday_2026_is_closed(self):
        # Easter 2026 is April 5 -- Good Friday is April 3.
        assert is_trading_day(date(2026, 4, 3)) is False
        # The surrounding Thursday and Monday are ordinary trading days.
        assert is_trading_day(date(2026, 4, 2)) is True
        assert is_trading_day(date(2026, 4, 6)) is True

    def test_good_friday_2027_is_closed_different_date_each_year(self):
        # Easter 2027 is March 28 -- Good Friday is March 26. Proves
        # this isn't a hardcoded single date, but computed per year.
        assert is_trading_day(date(2027, 3, 26)) is False

    def test_memorial_day_2026_is_closed(self):
        # Last Monday in May 2026 = May 25.
        assert is_trading_day(date(2026, 5, 25)) is False

    def test_juneteenth_2026_is_closed(self):
        assert is_trading_day(date(2026, 6, 19)) is False

    def test_independence_day_2026_is_closed(self):
        assert is_trading_day(date(2026, 7, 4)) is False  # a Saturday in 2026 -- inherently closed anyway

    def test_independence_day_observed_when_falling_on_saturday(self):
        # July 4 2026 is a Saturday -- observed the preceding Friday, July 3.
        assert date(2026, 7, 4).weekday() == 5
        assert is_trading_day(date(2026, 7, 3)) is False

    def test_labor_day_2026_is_closed(self):
        # 1st Monday in September 2026 = Sep 7.
        assert is_trading_day(date(2026, 9, 7)) is False

    def test_thanksgiving_2026_is_closed(self):
        # 4th Thursday in November 2026 = Nov 26.
        assert date(2026, 11, 26).weekday() == 3
        assert is_trading_day(date(2026, 11, 26)) is False

    def test_christmas_2026_is_closed(self):
        assert is_trading_day(date(2026, 12, 25)) is False


class TestEarlyCloseSessions:
    def test_day_after_thanksgiving_2026_is_an_early_close_trading_day(self):
        assert is_trading_day(date(2026, 11, 27)) is True
        assert is_early_close(date(2026, 11, 27)) is True
        assert regular_close(date(2026, 11, 27)).time().isoformat() == "13:00:00"

    def test_christmas_eve_2026_thursday_is_an_early_close_trading_day(self):
        assert is_trading_day(date(2026, 12, 24)) is True
        assert is_early_close(date(2026, 12, 24)) is True

    def test_ordinary_trading_day_is_never_flagged_early_close(self):
        assert is_early_close(date(2026, 9, 21)) is False

    def test_a_holiday_itself_is_never_flagged_early_close(self):
        assert is_early_close(date(2026, 12, 25)) is False


class TestBeforeAtDuringAndAfterMarketHours:
    """All against 2026-09-21, a Monday with no DST edge case, 9:30am-4pm ET."""

    def test_before_market_open(self):
        assert is_market_open(et(2026, 9, 21, 9, 0)) is False

    def test_exactly_at_open(self):
        assert is_market_open(et(2026, 9, 21, 9, 30)) is True

    def test_normal_mid_session(self):
        assert is_market_open(et(2026, 9, 21, 12, 0)) is True

    def test_exactly_at_close_is_not_open(self):
        # [open, close) -- the closing instant itself is not "open."
        assert is_market_open(et(2026, 9, 21, 16, 0)) is False

    def test_after_close(self):
        assert is_market_open(et(2026, 9, 21, 17, 0)) is False

    def test_weekend_is_never_open_regardless_of_hour(self):
        assert is_market_open(et(2026, 9, 26, 12, 0)) is False

    def test_holiday_is_never_open_regardless_of_hour(self):
        assert is_market_open(et(2026, 12, 25, 12, 0)) is False

    def test_early_close_session_is_closed_after_1pm(self):
        assert is_market_open(et(2026, 11, 27, 13, 30)) is False
        assert is_market_open(et(2026, 11, 27, 12, 0)) is True


class TestDstTransitionAndUtcEasternConversion:
    def test_spring_forward_2026_march_8_open_time_shifts_in_utc(self):
        # 2026 DST starts the 2nd Sunday in March = March 8.
        before = regular_open(date(2026, 3, 6))  # Friday, still EST (UTC-5)
        after = regular_open(date(2026, 3, 9))  # Monday, now EDT (UTC-4)
        assert before.astimezone(timezone.utc).hour == 14  # 9:30 EST = 14:30 UTC
        assert after.astimezone(timezone.utc).hour == 13  # 9:30 EDT = 13:30 UTC

    def test_fall_back_2026_november_1_close_time_shifts_in_utc(self):
        # 2026 DST ends the 1st Sunday in November = November 1.
        before = regular_close(date(2026, 10, 30))  # Friday, still EDT
        after = regular_close(date(2026, 11, 2))  # Monday, now EST
        assert before.astimezone(timezone.utc).hour == 20  # 16:00 EDT = 20:00 UTC
        assert after.astimezone(timezone.utc).hour == 21  # 16:00 EST = 21:00 UTC

    def test_a_utc_timestamp_is_correctly_evaluated_against_eastern_session_hours(self):
        # 2026-09-21 14:00 UTC = 10:00 ET (EDT, UTC-4) -- mid-session.
        dt_utc = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
        assert is_market_open(dt_utc) is True
        # 2026-09-21 12:00 UTC = 08:00 ET -- before open.
        dt_utc_early = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        assert is_market_open(dt_utc_early) is False


class TestNeverNaiveDatetimes:
    def test_is_market_open_rejects_naive_datetime(self):
        with pytest.raises(NaiveDatetimeError):
            is_market_open(datetime(2026, 9, 21, 10, 0))

    def test_next_market_open_rejects_naive_datetime(self):
        with pytest.raises(NaiveDatetimeError):
            next_market_open(datetime(2026, 9, 21, 10, 0))

    def test_current_session_date_rejects_naive_datetime(self):
        with pytest.raises(NaiveDatetimeError):
            current_session_date(datetime(2026, 9, 21, 10, 0))

    def test_market_status_rejects_naive_datetime(self):
        with pytest.raises(NaiveDatetimeError):
            market_status(datetime(2026, 9, 21, 10, 0))

    def test_require_market_open_for_execution_rejects_naive_datetime(self):
        with pytest.raises(NaiveDatetimeError):
            require_market_open_for_execution(datetime(2026, 9, 21, 10, 0))


class TestNextTradingDayAndNextMarketOpen:
    def test_next_trading_day_after_a_friday_is_monday(self):
        assert next_trading_day(date(2026, 9, 25)) == date(2026, 9, 28)

    def test_next_trading_day_skips_a_holiday(self):
        # Thursday Nov 25 2026 (day before Thanksgiving) -> next trading
        # day is NOT Thanksgiving (Nov 26), it's Nov 27.
        assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)

    def test_next_trading_day_skips_an_entire_holiday_weekend(self):
        # Christmas 2026 is a Friday -- next trading day skips the
        # weekend too, landing on Monday Dec 28.
        assert next_trading_day(date(2026, 12, 25)) == date(2026, 12, 28)

    def test_next_market_open_before_todays_open_is_today(self):
        result = next_market_open(et(2026, 9, 21, 6, 0))
        assert result == et(2026, 9, 21, 9, 30)

    def test_next_market_open_after_todays_close_is_tomorrow(self):
        result = next_market_open(et(2026, 9, 21, 18, 0))
        assert result.astimezone(EASTERN).date() == date(2026, 9, 22)
        assert result.astimezone(EASTERN).time().isoformat() == "09:30:00"

    def test_next_market_open_on_a_weekend_is_the_following_monday(self):
        result = next_market_open(et(2026, 9, 26, 12, 0))  # Saturday
        assert result.astimezone(EASTERN).date() == date(2026, 9, 28)  # Monday


class TestCurrentSessionDate:
    def test_returns_todays_date_during_market_hours(self):
        assert current_session_date(et(2026, 9, 21, 11, 0)) == date(2026, 9, 21)

    def test_returns_none_when_market_closed(self):
        assert current_session_date(et(2026, 9, 21, 20, 0)) is None
        assert current_session_date(et(2026, 9, 26, 11, 0)) is None  # Saturday


class TestMarketClosedFailsClosedForExecution:
    def test_require_market_open_raises_when_closed(self):
        with pytest.raises(MarketClosedError, match="market is closed"):
            require_market_open_for_execution(et(2026, 9, 26, 11, 0))  # Saturday

    def test_require_market_open_is_silent_when_open(self):
        require_market_open_for_execution(et(2026, 9, 21, 11, 0))  # no exception

    def test_require_market_open_raises_on_a_holiday(self):
        with pytest.raises(MarketClosedError):
            require_market_open_for_execution(et(2026, 12, 25, 11, 0))

    def test_require_market_open_raises_after_an_early_close(self):
        with pytest.raises(MarketClosedError):
            require_market_open_for_execution(et(2026, 11, 27, 14, 0))  # after 1pm early close


class TestMarketStatusSnapshot:
    def test_full_snapshot_during_a_normal_session(self):
        status = market_status(et(2026, 9, 21, 11, 0))
        assert status.is_trading_day is True
        assert status.is_market_open is True
        assert status.is_early_close_session is False
        assert status.regular_open == et(2026, 9, 21, 9, 30)
        assert status.regular_close == et(2026, 9, 21, 16, 0)
        assert status.next_trading_day == date(2026, 9, 21)

    def test_full_snapshot_on_a_holiday(self):
        status = market_status(et(2026, 12, 25, 11, 0))
        assert status.is_trading_day is False
        assert status.is_market_open is False
        assert status.regular_open is None
        assert status.regular_close is None
        assert status.next_trading_day == date(2026, 12, 28)  # skips the Fri holiday + weekend
