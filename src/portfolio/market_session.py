"""PAPER_TRADING_V1.5.1, Step 2: the new-position daily validation
cycle's market-hours safety gate.

**Why this exists.** On 2026-09-25 the operator was able to click "Run
Daily Validation" at roughly 9:21 AM ET -- before the regular session
even opened -- and the daily cycle started scanning for new-position
candidates against pre-market data. The existing data-quality/
freshness machinery (`src.data.quality_gate`, `TimestampedModel`) still
failed safely (no bad quote was ever treated as executable, no order
was ever placed), but the cycle should never have been allowed to
START as an ordinary opportunity scan that early at all. This module
is the fix: a deterministic, timezone-aware, config-driven decision of
whether *today's new-position opportunity scan* may run right now --
evaluated and enforced BEFORE any market-data provider call, before
candidate generation, before Quant/Risk evaluation of a new candidate,
and before any new cycle/snapshot record is persisted (see
`scripts.run_validation_cycle.run_validation_cycle`, the sole place
this module's decision is enforced).

**This module computes ONE thing: whether the automated daily
new-position scan may start.** It is deliberately NOT a second market
calendar -- every calendar/session-timing primitive (holidays, early
closes, timezone conversion, `[open, close)` session membership) comes
from the existing, already-frozen `src.data.market_calendar` module,
reused here without modification and without any parallel/competing
reimplementation. This module only adds the POLICY layer on top:
today's session state in the small vocabulary a dashboard needs
(`MarketSessionState`), and a scan-eligibility decision that additionally
respects a configurable "wait a few minutes after the open, stop a few
minutes before the close" buffer that `src.data.market_calendar` itself
has no opinion about (staleness/freshness tolerance and "is the market
literally open right now" are different questions from "is it a good
idea to START today's automated new-position scan right now").

**Existing-position lifecycle/risk monitoring is a completely separate
concern and is NEVER touched by this module.** `src.portfolio
.control_loop.run_control_cycle` (Lifecycle Engine + Risk kill-switch
for already-open positions) has exactly one production call site
(`scripts.run_validation_cycle.run_validation_cycle`, via
`src.portfolio.orchestrator.run_outer_cycle`) and never gates on
market hours internally -- `ControlCycleInputs.is_market_open` is
carried through only as descriptive metadata on the resulting
`ControlCycleRecord`/decision snapshots, never used to skip evaluating
a single position. This module's decision governs only whether that
one daily script/endpoint invocation is allowed to *start* at all; it
never reaches into, modifies, or adds a gate inside
`run_control_cycle`, `evaluate_position`, or `check_kill_switch`
themselves, so none of those remain fully intact, unmodified, and
independently callable/testable by any future caller, exactly as they
were before this step. See `scripts/run_validation_cycle.py`'s module
docstring for the full reasoning on why gating the *whole* daily
invocation (rather than only its opportunity-scan sub-stage) is the
safe choice here, given that `run_control_cycle` itself unconditionally
persists a `ControlCycleRecord` (consuming that day's cycle-level
idempotency slot) the moment it runs.

**Fail-closed on calendar uncertainty.** If `src.data.market_calendar`
cannot determine today's session (any unexpected exception from its own
holiday/session computation), `evaluate_validation_cycle_eligibility`
never assumes the market is open -- it returns
`validation_cycle_allowed=False` with an explicit reason. A naive
(timezone-unaware) `now` is not "calendar uncertainty" -- it is a
caller error, and is rejected exactly the way every other market-hours
function in this codebase rejects one (`src.data.market_calendar
.NaiveDatetimeError`, allowed to propagate here unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.data import market_calendar

# Step 2's own documented policy defaults, used only when a caller does
# not supply its own (config-loaded) buffer -- see
# `src.portfolio.operations_config.OperationsConfig.scan_open_buffer_minutes`
# /`scan_close_buffer_minutes` for the real, operator-configurable
# values `scripts/run_validation_cycle.py` actually uses.
#
# No pre-existing "market open buffer"/"data stabilization buffer"
# policy exists anywhere in this repository (confirmed by inspection
# before this module was written: `src.data.provider.DEFAULT_MAX_QUOTE_AGE`
# and `src.llm.schemas`/`src.brokers.fidelity`'s `MAX_MARKET_DATA_AGE`,
# both 15 minutes, are QUOTE-STALENESS tolerances -- "how old may a
# quote be and still be trusted" -- a different question from "how long
# after the bell should a brand-new scan wait before starting"). These
# defaults are therefore a documented, minimal, conservative POLICY
# choice, not a value derived from anything already in this codebase:
#   - 5 minutes after the open: options-market opening-auction prints
#     and NBBO quotes are widely understood to be less reliable in the
#     first few minutes of the regular session; 5 minutes is a common,
#     conservative rule of thumb for letting that settle before a new
#     automated scan trusts the data it sees.
#   - 15 minutes before the close: deliberately matches this platform's
#     own existing 15-minute quote-staleness tolerance above, so a scan
#     is never started so close to the closing bell that a quote it
#     just fetched could already be stale relative to that same
#     tolerance by the time it is used.
DEFAULT_SCAN_OPEN_BUFFER_MINUTES = 5
DEFAULT_SCAN_CLOSE_BUFFER_MINUTES = 15


class MarketSessionState(str, Enum):
    """The small, dashboard-facing session vocabulary Step 2 asks for.
    Deliberately does not add a distinct EARLY_CLOSE state -- exactly
    like `market_calendar.MarketStatus.is_early_close_session`, an
    early close is represented as a boolean fact about a trading day,
    not a fifth session label, so `regular_session_close` already
    reflects the true (earlier) closing time whenever `is_trading_day`
    is an early-close session -- no separate state is needed merely for
    display."""

    MARKET_CLOSED = "MARKET_CLOSED"
    PRE_MARKET = "PRE_MARKET"
    REGULAR_MARKET = "REGULAR_MARKET"
    POST_MARKET = "POST_MARKET"


@dataclass(frozen=True)
class ValidationCycleEligibility:
    """One deterministic snapshot answering everything
    `scripts.run_validation_cycle.run_validation_cycle` and the
    dashboard's `GET /api/operator-status` need: today's raw exchange
    session state (for display), and the separate, buffer-adjusted
    `validation_cycle_allowed` decision (for the actual gate) plus a
    human-readable reason whenever it's `False`."""

    as_of: datetime
    market_session_state: MarketSessionState
    is_trading_day: bool
    regular_session_open: datetime | None
    regular_session_close: datetime | None
    validation_cycle_allowed: bool
    block_reason: str | None


def _session_state(now: datetime, status: market_calendar.MarketStatus) -> MarketSessionState:
    if not status.is_trading_day:
        return MarketSessionState.MARKET_CLOSED
    # `status.regular_open`/`regular_close` are guaranteed non-None here
    # -- `market_calendar.regular_open`/`regular_close` only return
    # `None` for a non-trading day, already ruled out above.
    if now < status.regular_open:  # type: ignore[operator]
        return MarketSessionState.PRE_MARKET
    if status.is_market_open:
        return MarketSessionState.REGULAR_MARKET
    return MarketSessionState.POST_MARKET


def evaluate_validation_cycle_eligibility(
    now: datetime,
    *,
    scan_open_buffer_minutes: int = DEFAULT_SCAN_OPEN_BUFFER_MINUTES,
    scan_close_buffer_minutes: int = DEFAULT_SCAN_CLOSE_BUFFER_MINUTES,
) -> ValidationCycleEligibility:
    """The one function `scripts.run_validation_cycle.run_validation_cycle`
    and `src.dashboard.validation_ops.build_operator_status` both call to
    decide/display whether today's new-position daily validation cycle
    may start right now. Pure and deterministic: the same `now` (in any
    timezone -- it is converted internally, exactly like every
    `src.data.market_calendar` function) always produces the same
    result, and nothing here reads a clock, a file, or the network.

    Raises `market_calendar.NaiveDatetimeError` for a naive `now` --
    a caller bug, not a runtime "market is uncertain" condition, so it
    is never silently converted to a fail-closed result; it propagates
    exactly like every other market-hours function in this codebase.
    """
    try:
        status = market_calendar.market_status(now)
    except market_calendar.NaiveDatetimeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- fail closed on ANY unexpected calendar failure
        return ValidationCycleEligibility(
            as_of=now,
            market_session_state=MarketSessionState.MARKET_CLOSED,
            is_trading_day=False,
            regular_session_open=None,
            regular_session_close=None,
            validation_cycle_allowed=False,
            block_reason=f"market calendar could not determine today's session -- {exc!r}",
        )

    session_state = _session_state(now, status)

    if not status.is_trading_day:
        return ValidationCycleEligibility(
            as_of=now,
            market_session_state=session_state,
            is_trading_day=False,
            regular_session_open=None,
            regular_session_close=None,
            validation_cycle_allowed=False,
            block_reason="today is not a trading day (weekend or U.S. market holiday)",
        )

    regular_open = status.regular_open
    regular_close = status.regular_close
    assert regular_open is not None and regular_close is not None  # guaranteed on a trading day

    scan_window_open = regular_open + timedelta(minutes=scan_open_buffer_minutes)
    scan_window_close = regular_close - timedelta(minutes=scan_close_buffer_minutes)

    if now < scan_window_open:
        allowed = False
        if session_state is MarketSessionState.PRE_MARKET:
            reason = (
                f"pre-market -- regular session opens at {regular_open.isoformat()}, "
                f"the new-position scan window opens at {scan_window_open.isoformat()}"
            )
        else:
            reason = (
                f"within the post-open stabilization buffer -- "
                f"the new-position scan window opens at {scan_window_open.isoformat()}"
            )
    elif now >= scan_window_close:
        allowed = False
        if session_state is MarketSessionState.POST_MARKET:
            reason = f"post-market -- regular session closed at {regular_close.isoformat()}"
        else:
            reason = (
                f"within the pre-close buffer -- "
                f"the new-position scan window closed at {scan_window_close.isoformat()}"
            )
    else:
        allowed = True
        reason = None

    return ValidationCycleEligibility(
        as_of=now,
        market_session_state=session_state,
        is_trading_day=True,
        regular_session_open=regular_open,
        regular_session_close=regular_close,
        validation_cycle_allowed=allowed,
        block_reason=reason,
    )
