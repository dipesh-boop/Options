"""Step 22 Part 7-9: a deterministic U.S. equity/options market-hours
and holiday calendar safeguard.

**Why stdlib-only, not a third-party exchange-calendar library**: this
platform has never taken a dependency on `pandas` anywhere (confirmed
by `requirements.txt` — `numpy` is a direct dependency, `pandas` never
has been, despite extensive numeric/time-series work throughout
`src/quant`/`src/backtest`), and `pandas_market_calendars` pulls in
`pandas` plus `exchange_calendars` plus two unrelated lunar-calendar
packages (`pyluach`, `korean_lunar_calendar`) as transitive
dependencies — a large footprint change to a codebase's dependency
surface, right before a V1.0 freeze, for a single-market (NYSE/Nasdaq)
calendar need. This module instead computes the NYSE holiday schedule
from published, stable observance RULES (federal-holiday Saturday/
Sunday shifting, the well-known Meeus/Jones/Butcher algorithm for
Easter/Good Friday, and NYSE's own published early-close rules) using
only `datetime`/`zoneinfo` (both stdlib since Python 3.9) — fully
deterministic, correctly DST-aware via `zoneinfo`'s IANA tzdata, and
covering any calendar year without a hardcoded date table that could
silently run out.

**What this module is NOT**: a certified, exhaustively-verified
duplicate of NYSE's actual published calendar for every historical
edge case (e.g. a one-off historical market closure for a national day
of mourning). It is a SAFEGUARD against the specific failure mode
Step 21 flagged — accidentally treating a weekend or a major U.S.
holiday as an ordinary trading session — not a substitute for a
real-time exchange status feed in a live-trading system (this platform
has no live-trading system; see CLAUDE.md invariant #7).

Every function here either takes a `date` (calendar-only questions) or
a timezone-AWARE `datetime` (session-state questions) — a naive
datetime is always rejected outright, never silently assumed to be any
particular timezone. See `require_market_open_for_execution` for the
"never fabricate a current executable price when closed" enforcement
point Part 8 asks for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_REGULAR_OPEN_TIME = time(9, 30)
_REGULAR_CLOSE_TIME = time(16, 0)
_EARLY_CLOSE_TIME = time(13, 0)


class NaiveDatetimeError(ValueError):
    """A market-session question was asked with a timezone-naive
    datetime — refused outright rather than silently assumed to be any
    particular timezone (Eastern, UTC, or otherwise). Every caller must
    be explicit; see Step 21's `_check_timezone_aware` precedent this
    module follows throughout the rest of the codebase."""


def _require_aware(dt: datetime, name: str = "dt") -> None:
    if dt.tzinfo is None:
        raise NaiveDatetimeError(f"{name} must be timezone-aware, got a naive datetime: {dt!r}")


# ------------------------------------------------------------- holidays


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """`weekday`: Monday=0 .. Sunday=6. `n`: 1-indexed occurrence."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += timedelta(days=offset)
    return d + timedelta(weeks=n - 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    d = next_month_first - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Meeus/Jones/Butcher Gregorian algorithm — the standard,
    deterministic closed-form computation for the date of Easter,
    accurate for any Gregorian-calendar year. Good Friday (an NYSE
    holiday, though not a federal one) is exactly 2 days before this."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """Standard federal-holiday weekend-observance shift: a holiday
    falling on Saturday is observed the preceding Friday; on Sunday,
    the following Monday. NYSE follows this same convention for its
    fixed-date holidays (New Year's, Juneteenth, Independence Day,
    Christmas)."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def _nyse_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # MLK Day: 3rd Monday in January
        _nth_weekday_of_month(year, 2, 0, 3),  # Presidents Day: 3rd Monday in February
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day: last Monday in May
        _observed(date(year, 6, 19)),  # Juneteenth (NYSE holiday since 2022)
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day: 1st Monday in September
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving: 4th Thursday in November
        _observed(date(year, 12, 25)),  # Christmas Day
    }
    # New Year's Day observance can shift BACKWARD into December of the
    # PRECEDING year (Jan 1 falling on a Saturday is observed the
    # Friday before, i.e. Dec 31 of the previous year) -- a lookup for
    # that Dec 31 date, keyed by its own (previous) year, would
    # otherwise miss it entirely, since every other holiday above is
    # computed and checked within the same calendar year it occurs in.
    next_new_years_observed = _observed(date(year + 1, 1, 1))
    if next_new_years_observed.year == year:
        holidays.add(next_new_years_observed)
    return holidays


def _nyse_early_closes(year: int) -> set[date]:
    """NYSE's published early-close (1:00pm ET) sessions: the day
    after Thanksgiving (always), Christmas Eve when it falls on a
    weekday, and July 3rd when it falls Monday-Thursday (i.e. when
    Independence Day itself is NOT being observed that same day and
    July 3rd is a real trading day). A handful of historical one-off
    early closes (e.g. 9/11's anniversary observances) are not modeled
    — this is a safeguard against ordinary calendar mistakes, not a
    certified historical archive."""
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    early = {thanksgiving + timedelta(days=1)}

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5:  # Mon-Fri
        early.add(christmas_eve)

    july_3 = date(year, 7, 3)
    if july_3.weekday() < 4:  # Mon-Thu (Friday would make July 4th the same week's Friday holiday, no separate early close needed distinctly)
        early.add(july_3)

    return early


# --------------------------------------------------------- calendar API


def is_trading_day(d: date) -> bool:
    """Weekends and NYSE holidays are never trading days — this is the
    single check every other function in this module ultimately
    reduces to."""
    if d.weekday() >= 5:  # Saturday/Sunday
        return False
    return d not in _nyse_holidays(d.year)


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    return d in _nyse_early_closes(d.year)


def regular_open(d: date) -> datetime | None:
    """`None` if `d` is not a trading day at all — never a fabricated
    open time for a closed day."""
    if not is_trading_day(d):
        return None
    return datetime.combine(d, _REGULAR_OPEN_TIME, tzinfo=EASTERN)


def regular_close(d: date) -> datetime | None:
    """`None` if `d` is not a trading day. Accounts for early closes
    (1:00pm ET) automatically."""
    if not is_trading_day(d):
        return None
    close_time = _EARLY_CLOSE_TIME if is_early_close(d) else _REGULAR_CLOSE_TIME
    return datetime.combine(d, close_time, tzinfo=EASTERN)


def next_trading_day(d: date) -> date:
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def next_market_open(dt: datetime) -> datetime:
    """The next regular-session open at or after `dt`. If `dt` itself
    falls before today's open on a trading day, that same day's open is
    returned (not tomorrow's)."""
    _require_aware(dt)
    dt_eastern = dt.astimezone(EASTERN)
    today = dt_eastern.date()
    today_open = regular_open(today)
    if today_open is not None and dt_eastern <= today_open:
        return today_open.astimezone(dt.tzinfo)
    nxt = next_trading_day(today)
    return regular_open(nxt).astimezone(dt.tzinfo)  # type: ignore[union-attr]


def is_market_open(dt: datetime) -> bool:
    """Whether `dt` falls strictly within a regular (or early-close)
    trading session — `[open, close)`, matching real exchange
    convention that the closing print happens exactly at close, not a
    moment after it."""
    _require_aware(dt)
    dt_eastern = dt.astimezone(EASTERN)
    today = dt_eastern.date()
    open_dt = regular_open(today)
    close_dt = regular_close(today)
    if open_dt is None or close_dt is None:
        return False
    return open_dt <= dt_eastern < close_dt


def current_session_date(dt: datetime) -> date | None:
    """The trading-session calendar date `dt` falls within, or `None`
    if the market is closed at `dt` (weekend, holiday, or simply
    outside today's regular hours) — deliberately distinct from
    `dt`'s own calendar date, which could be a non-trading day
    entirely."""
    _require_aware(dt)
    if not is_market_open(dt):
        return None
    return dt.astimezone(EASTERN).date()


@dataclass(frozen=True)
class MarketStatus:
    """A single, explicit snapshot answering every question Part 7
    names, computed once and passed around rather than re-derived
    piecemeal by every caller."""

    as_of: datetime
    is_trading_day: bool
    is_market_open: bool
    is_early_close_session: bool
    regular_open: datetime | None
    regular_close: datetime | None
    next_trading_day: date
    next_market_open: datetime


def market_status(dt: datetime) -> MarketStatus:
    _require_aware(dt)
    dt_eastern = dt.astimezone(EASTERN)
    today = dt_eastern.date()
    trading_today = is_trading_day(today)
    return MarketStatus(
        as_of=dt,
        is_trading_day=trading_today,
        is_market_open=is_market_open(dt),
        is_early_close_session=is_early_close(today) if trading_today else False,
        regular_open=regular_open(today),
        regular_close=regular_close(today),
        # "The next trading day" as a per-day question: today itself,
        # if today is a trading day at all -- independent of whether
        # today's session has already closed by `dt` (that's what
        # `next_market_open` is for).
        next_trading_day=today if trading_today else next_trading_day(today),
        next_market_open=next_market_open(dt),
    )


class MarketClosedError(RuntimeError):
    """Raised by `require_market_open_for_execution` — Part 8's own
    enforcement point. A trade candidate may still be REVIEWED using
    properly-labeled historical/last-known data while the market is
    closed (research, reporting, backtesting are never blocked by
    this), but nothing may represent stale data as a currently
    executable price while this exception would fire."""


def require_market_open_for_execution(dt: datetime) -> None:
    """The one function a workflow that is about to treat a quote as
    CURRENTLY EXECUTABLE (not merely last-known/historical) must call
    first. Raises `MarketClosedError` — fails closed, exactly like
    `OptionContract.assert_tradable` already does for stale data —
    rather than returning a boolean a caller could silently ignore."""
    _require_aware(dt)
    if not is_market_open(dt):
        status = market_status(dt)
        raise MarketClosedError(
            f"market is closed at {dt.isoformat()} (Eastern: {dt.astimezone(EASTERN).isoformat()}) -- "
            f"cannot treat any quote as currently executable; next open is {status.next_market_open.isoformat()}"
        )
