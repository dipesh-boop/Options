"""PAPER_TRADING_V1.5.11 acceptance test: a manually-initiated existing-
position lifecycle safety recheck may now run more than once per
trading day, bucketed by the market-local (America/New_York) clock
hour -- a duplicate/retry guard, never a scheduler. New-position
scanning remains at most once per trading day; the main `validation-
{date}` cycle id and its own idempotency check are completely
unmodified. Exercises the REAL production entry point,
`scripts/run_validation_cycle.py`'s own `run_validation_cycle()` (and,
for the dashboard item, the dashboard's `POST /api/validation-cycle/run`
route), against deterministic, explicitly-injected `now` values --
never the real wall clock, never a live Tradier call.

Covers the 22 items PAPER_TRADING_V1.5.11's own task spec requires; see
each test class's own docstring for which item(s) it satisfies.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.data.market_calendar import EASTERN
from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType
from src.portfolio.account_state import SqlitePortfolioStore
from src.portfolio.persistence import SqliteControlLoopStore
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from tests.acceptance.test_lifecycle_exact_expiration_retrieval import (
    _DEFAULT_LEGS,
    _DenseExpirationFakeProvider,
    _contracts_for,
    _evaluated,
    _seed_positions,
)
from tests.acceptance.test_lifecycle_market_hours_separation import (
    _ALLOWED,
    _BLOCKED,
    SCRIPT_PATH,
    _idempotency_store,
    _patch_env,
    _review_store,
)
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

# A fixed EDT (summer, UTC-4) calendar date -- real wall-clock-independent,
# never used to infer "today" anywhere in production code (every `now`
# below is explicitly injected). October 6, 2026 is a Tuesday, well
# before the US DST "fall back" (first Sunday of November).
_EDT_DATE = date(2026, 10, 6)
# A fixed EST (winter, UTC-5) calendar date, for the DST-boundary item.
_EST_DATE = date(2026, 1, 6)


def _dense_near_term_slices_on(ticker: str, today: date, *, exclude_dte: set[int] = frozenset()) -> list[tuple[date, list]]:
    """Local, explicitly-dated equivalent of `test_lifecycle_exact_
    expiration_retrieval._dense_near_term_slices` -- that module's
    version anchors every expiration to its own `_TODAY` (the REAL wall
    clock date at import time), which this file must never depend on:
    every `now` here is an explicitly injected, simulated instant
    (`_EDT_DATE`/`_EST_DATE`), and a held position's own `expiration`
    (built by this file's `_position_at_dte`, from that SAME simulated
    `as_of`) must land on exactly the date basis as the provider's
    slices, or an exact-DTE fetch silently misses by a day purely as an
    artifact of this fake, never a real data gap the test intended."""
    out = []
    for dte in (1, 3, 5, 7, 9, 11):
        if dte in exclude_dte:
            continue
        expiration = today + timedelta(days=dte)
        out.append((expiration, _contracts_for(ticker, expiration, [(400.0, OptionRight.PUT)], as_of=datetime.now(timezone.utc))))
    return out


def _held_slice_on(ticker: str, today: date, dte: int, specs: list[tuple[float, OptionRight]] = None) -> tuple[date, list]:
    specs = specs if specs is not None else _DEFAULT_LEGS
    expiration = today + timedelta(days=dte)
    return expiration, _contracts_for(ticker, expiration, specs, as_of=datetime.now(timezone.utc))


def _utc_for_eastern(d: date, hour: int, minute: int = 0) -> datetime:
    """Builds a UTC `datetime` that corresponds to `hour:minute` ET on
    date `d` -- derived from the REAL `America/New_York` zoneinfo
    (never a hardcoded fixed offset), so this helper is correct across
    both EDT and EST without the test needing to know which applies."""
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=EASTERN)
    return local.astimezone(timezone.utc)


def _main_cycle_id_for(now: datetime) -> str:
    return f"validation-{now.date().isoformat()}"


def _lifecycle_cycle_id_for(now: datetime) -> str:
    local = now.astimezone(EASTERN)
    return f"validation-{local.date().isoformat()}-lifecycle-{local.hour:02d}"


def _position_at_dte(
    position_id: str, ticker: str, dte: int, *, as_of: datetime, legs=None,
) -> PortfolioPosition:
    legs = legs if legs is not None else _DEFAULT_LEGS
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
        expiration=as_of.date() + timedelta(days=dte),
        legs=[
            PortfolioPositionLeg(right=right.value, side="sell" if i == 0 else "buy", strike=strike, entry_price=6.0 - i * 3.0)
            for i, (strike, right) in enumerate(legs)
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0,
        opened_at=as_of - timedelta(days=1),
    )


def _provider_for(ticker: str, dte: int, *, reference_now: datetime, today: date = _EDT_DATE) -> _DenseExpirationFakeProvider:
    return _DenseExpirationFakeProvider(
        slices_by_ticker={
            ticker: [*_dense_near_term_slices_on(ticker, today, exclude_dte={dte}), _held_slice_on(ticker, today, dte)],
        },
        reference_now=reference_now,
    )


async def _run(cycle, provider, now: datetime) -> bool:
    """Every test below reuses ONE fake provider across several
    `run_validation_cycle(now=...)` calls at different simulated
    instants (e.g. a 10am main cycle, then a 1pm hourly recheck) --
    this keeps the provider's own stamped data anchored to whichever
    simulated `now` that specific call represents, never the real wall
    clock and never a stale instant left over from an earlier call."""
    if provider is not None:
        provider.set_reference_now(now)
    return await cycle.run_validation_cycle(now=now)


@pytest.mark.asyncio
class TestFreshHourlyRecheckAfterMainCycle:
    """Items 1, 17: main cycle at 10:00 ET, lifecycle recheck at 13:00 ET
    -> a fresh lifecycle evaluation occurs, and the main cycle's own
    at-most-once-per-day guarantee is unaffected (no second opportunity
    scan/candidate)."""

    async def test_recheck_two_hours_later_fetches_fresh_and_evaluates(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1pm = _utc_for_eastern(_EDT_DATE, 13)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_10am))
        provider = _provider_for("SPY", 30, reference_now=now_10am)
        cycle = _load_script_module("_v1511_fresh_recheck", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok1 = await _run(cycle, provider, now_10am)
        assert ok1 is True
        calls_after_main = len(provider.get_option_chain_for_dte_window_calls)

        ok2 = await _run(cycle, provider, now_1pm)
        assert ok2 is True
        assert len(provider.get_option_chain_for_dte_window_calls) > calls_after_main, (
            "the 1pm recheck must perform its own fresh fetch, not reuse the 10am result"
        )

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        main_id = _main_cycle_id_for(now_10am)
        assert control_loop_store.get_cycle_record(main_id) is not None
        recheck_id = _lifecycle_cycle_id_for(now_1pm)
        assert control_loop_store.get_cycle_record(recheck_id) is not None
        assert _evaluated(control_loop_store, recheck_id, "p1")

        # Item 17: still at most one opportunity scan this trading day --
        # no second candidate, no second scan-eligible cycle record.
        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []


@pytest.mark.asyncio
class TestSameHourSecondInvocationIsNoOp:
    """Item 2: main cycle at 10:00, lifecycle recheck TWICE within the
    13:xx hour -> the first runs, the second is a documented no-op (no
    additional provider call, no additional snapshot)."""

    async def test_second_call_within_the_same_hour_bucket_no_ops(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1pm_a = _utc_for_eastern(_EDT_DATE, 13, 5)
        now_1pm_b = _utc_for_eastern(_EDT_DATE, 13, 47)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_10am))
        provider = _provider_for("SPY", 30, reference_now=now_10am)
        cycle = _load_script_module("_v1511_same_hour_twice", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        await _run(cycle, provider, now_10am)
        await _run(cycle, provider, now_1pm_a)
        calls_after_first_recheck = len(provider.get_option_chain_for_dte_window_calls)

        ok = await _run(cycle, provider, now_1pm_b)
        assert ok is True
        assert len(provider.get_option_chain_for_dte_window_calls) == calls_after_first_recheck, (
            "a second call in the SAME hour bucket must never re-fetch"
        )

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1pm_a)
        assert recheck_id == _lifecycle_cycle_id_for(now_1pm_b)
        snapshots = control_loop_store.decision_snapshots_for_cycle(recheck_id)
        assert len(snapshots) == 1, "no duplicate snapshot from the second same-hour call"


@pytest.mark.asyncio
class TestDistinctHourBuckets:
    """Item 3, 20: a check at 13:59 ET then one at 14:00 ET -> two
    distinct buckets, two fresh evaluations -- the boundary is computed
    from the real America/New_York zoneinfo, never a naive UTC hour."""

    async def test_1359_then_1400_are_two_distinct_buckets(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_1359 = _utc_for_eastern(_EDT_DATE, 13, 59)
        now_1400 = _utc_for_eastern(_EDT_DATE, 14, 0)
        assert _lifecycle_cycle_id_for(now_1359) != _lifecycle_cycle_id_for(now_1400), (
            "13:59 and 14:00 ET must fall into different hourly buckets"
        )

        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_1359))
        provider = _provider_for("SPY", 30, reference_now=now_1359)
        cycle = _load_script_module("_v1511_hour_boundary", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_1359)
        calls_after_first = len(provider.get_option_chain_for_dte_window_calls)
        await _run(cycle, provider, now_1400)
        assert len(provider.get_option_chain_for_dte_window_calls) > calls_after_first, (
            "crossing into a new hour bucket must trigger a fresh fetch"
        )

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id_for(now_1359)) is not None
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id_for(now_1400)) is not None


class TestDstBoundaryAcrossSeasons:
    """Item 20: the hourly bucket is computed from real America/New_York
    local time in BOTH EDT (summer) and EST (winter), handled entirely
    by `zoneinfo` -- never a fixed offset that would silently misalign
    across a DST transition."""

    def test_bucket_hour_matches_real_eastern_local_time_in_both_seasons(self):
        for d in (_EDT_DATE, _EST_DATE):
            now = _utc_for_eastern(d, 11, 30)
            expected_local_hour = now.astimezone(ZoneInfo("America/New_York")).hour
            assert expected_local_hour == 11
            assert _lifecycle_cycle_id_for(now) == f"validation-{d.isoformat()}-lifecycle-11"


@pytest.mark.asyncio
class TestGateClosedPreservesMainDailyId:
    """Items 4, 5: gate closed at 08:30 ET with a position -> lifecycle
    runs but the main daily cycle id remains untouched; gate open later
    at 10:30 ET -> the normal daily cycle still runs."""

    async def test_morning_lifecycle_then_later_normal_cycle(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1030 = _utc_for_eastern(_EDT_DATE, 10, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        provider = _provider_for("SPY", 30, reference_now=now_830)
        cycle = _load_script_module("_v1511_premarket_then_open", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)

        eligibility_state = {"allowed": False}
        monkeypatch.setattr(
            cycle, "evaluate_validation_cycle_eligibility",
            lambda now, **kw: _ALLOWED if eligibility_state["allowed"] else _BLOCKED,
        )

        ok1 = await _run(cycle, provider, now_830)
        assert ok1 is True
        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        main_id = _main_cycle_id_for(now_830)
        assert control_loop_store.get_cycle_record(main_id) is None, "the premarket lifecycle check must never consume the main daily id"
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id_for(now_830)) is not None

        eligibility_state["allowed"] = True
        ok2 = await _run(cycle, provider, now_1030)
        assert ok2 is True
        assert control_loop_store.get_cycle_record(main_id) is not None, "the normal daily cycle must still be able to run once the window opens"


@pytest.mark.asyncio
class TestMainCycleRanThenGateClosesAgain:
    """Item 6: main daily cycle already ran, gate later closed, position
    exists -> the lifecycle hourly recheck still runs (via the pre-
    existing, unmodified gate-closed branch)."""

    async def test_main_cycle_then_post_close_lifecycle_recheck(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1530 = _utc_for_eastern(_EDT_DATE, 15, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_10am))
        provider = _provider_for("SPY", 30, reference_now=now_10am)
        cycle = _load_script_module("_v1511_post_close_recheck", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)

        eligibility_state = {"allowed": True}
        monkeypatch.setattr(
            cycle, "evaluate_validation_cycle_eligibility",
            lambda now, **kw: _ALLOWED if eligibility_state["allowed"] else _BLOCKED,
        )

        await _run(cycle, provider, now_10am)
        eligibility_state["allowed"] = False
        ok = await _run(cycle, provider, now_1530)
        assert ok is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1530)
        assert control_loop_store.get_cycle_record(recheck_id) is not None
        assert _evaluated(control_loop_store, recheck_id, "p1")


@pytest.mark.asyncio
class TestMainCycleRanNoPositionsLaterNoOp:
    """Item 7: main daily cycle already ran, no positions -> a later
    same-day invocation (gate still open) is a safe no-op."""

    async def test_no_positions_later_invocation_is_a_pure_no_op(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1pm = _utc_for_eastern(_EDT_DATE, 13)

        def _must_not_be_called():
            raise AssertionError("provider must never be constructed for a zero-position repeat invocation")

        cycle = _load_script_module("_v1511_no_positions_repeat", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        # First call bootstraps a zero-position portfolio for this
        # never-before-seen account under a real, offline-safe provider
        # -- then swap to the "must not be called" guard for the repeat
        # invocation this test actually cares about.
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        ok1 = await cycle.run_validation_cycle(now=now_10am)
        assert ok1 is True

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: _must_not_be_called())
        ok2 = await cycle.run_validation_cycle(now=now_1pm)
        assert ok2 is True

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id_for(now_1pm)) is None, (
            "no lifecycle record may be created when there are no positions to check"
        )


def _dte_exit_position(position_id: str, ticker: str, *, as_of: datetime, dte: int) -> PortfolioPosition:
    """A `dte` below PUT_CREDIT_SPREAD_STANDARD's own `forced_exit_dte`
    (21) -- `check_dte` fires a mandatory TIME_EXIT_TRIGGERED finding
    from `dte` alone, the only lifecycle trigger reachable through this
    operational path without caller-supplied `extra_monitoring_inputs`
    (which neither the main cycle nor the lifecycle-only path populates
    -- a pre-existing, unmodified characteristic of this call path, not
    something V1.5.11 changes). Callers that need EXACTLY one alert
    raised per cycle (dedup/no-auto-resolve tests) must pick a `dte`
    outside `[0, 7]` too -- `src.portfolio.alerts` independently raises
    `EXPIRATION_APPROACHING` for ANY snapshot with `0 <= dte <= 7`,
    regardless of what other alert type a TIME_EXIT/DTE_EXIT action
    already raised for the same position (pre-existing, unmodified
    behavior -- not something this test should second-guess)."""
    return _position_at_dte(position_id, ticker, dte, as_of=as_of)


@pytest.mark.asyncio
class TestUnresolvedAlertDedupAcrossHourlyChecks:
    """Item 8: the same unresolved lifecycle trigger (DTE forced-exit)
    across two hourly checks -> only one alert is ever raised, but each
    check still gets its own audit snapshot."""

    async def test_same_trigger_twice_one_alert_two_snapshots(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1pm = _utc_for_eastern(_EDT_DATE, 13)
        _seed_positions(environment, _dte_exit_position("p1", "SPY", as_of=now_10am, dte=15))
        provider = _provider_for("SPY", 15, reference_now=now_10am)
        cycle = _load_script_module("_v1511_alert_dedup", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_10am)
        await _run(cycle, provider, now_1pm)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        unresolved = [a for a in control_loop_store.all_unresolved_alerts() if a.scope == "p1"]
        assert len(unresolved) == 1, "the same still-unresolved (position_id, alert_type) condition must never duplicate an alert"

        snap_10am = control_loop_store.decision_snapshots_for_cycle(_lifecycle_cycle_id_for(now_10am))
        snap_1pm = control_loop_store.decision_snapshots_for_cycle(_lifecycle_cycle_id_for(now_1pm))
        assert len(snap_10am) == 1
        assert len(snap_1pm) == 1, "each hourly check still gets its own audit-trail snapshot, even for an unchanged condition"


@pytest.mark.asyncio
class TestAlertDoesNotAutoResolve:
    """Item 9: trigger true in the first hourly check, then false later
    -> this is DOCUMENTED, pre-existing behavior (OUT OF SCOPE for
    V1.5.11): the alert raised while the condition was true stays
    unresolved even after the condition clears. V1.5.11 does not add
    auto-resolution."""

    async def test_cleared_condition_leaves_the_earlier_alert_unresolved(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_10am = _utc_for_eastern(_EDT_DATE, 10)
        now_1pm = _utc_for_eastern(_EDT_DATE, 13)
        _seed_positions(environment, _dte_exit_position("p1", "SPY", as_of=now_10am, dte=15))
        provider = _provider_for("SPY", 15, reference_now=now_10am)
        cycle = _load_script_module("_v1511_alert_no_auto_resolve", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_10am)
        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        unresolved_after_first = [a for a in control_loop_store.all_unresolved_alerts() if a.scope == "p1"]
        assert len(unresolved_after_first) == 1

        # Simulate the trigger condition clearing: the position is now
        # held well above the forced-exit DTE threshold (as if it had
        # been rolled to a later expiration between checks).
        portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
        cleared_portfolio = Portfolio(
            as_of=now_1pm, nav=100_000.0, cash=80_000.0, peak_equity=100_000.0,
            positions=[_dte_exit_position("p1", "SPY", as_of=now_1pm, dte=30)],
            sector_by_ticker={"SPY": "ETF"},
        )
        portfolio_store.save(COHORT_ID, cleared_portfolio)
        provider_30 = _provider_for("SPY", 30, reference_now=now_1pm)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider_30)

        await _run(cycle, provider_30, now_1pm)

        unresolved_after_second = [a for a in control_loop_store.all_unresolved_alerts() if a.scope == "p1"]
        assert len(unresolved_after_second) == 1, (
            "documented pre-existing behavior: the earlier alert is never auto-resolved, even though the "
            "DTE condition that raised it no longer holds -- this is OUT OF SCOPE for V1.5.11"
        )


@pytest.mark.asyncio
class TestTwoExpirationsPreservedAcrossHourlyChecks:
    """Item 10: two same-ticker positions with different expirations ->
    V1.5.10's exact held-expiration retrieval is preserved across
    repeated hourly invocations."""

    async def test_both_expirations_retrieved_in_a_later_hourly_check(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(
            environment,
            _position_at_dte("p1", "SPY", 30, as_of=now_830),
            _position_at_dte("p2", "SPY", 10, as_of=now_830),
        )
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [*_dense_near_term_slices_on("SPY", _EDT_DATE, exclude_dte={11}), _held_slice_on("SPY", _EDT_DATE, 30), _held_slice_on("SPY", _EDT_DATE, 10)],
            },
            reference_now=now_830,
        )
        cycle = _load_script_module("_v1511_two_expirations_hourly", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        requested_windows = {(min_dte, max_dte) for _, min_dte, max_dte in provider.get_option_chain_for_dte_window_calls}
        assert (30, 30) in requested_windows
        assert (10, 10) in requested_windows

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1130)
        assert _evaluated(control_loop_store, recheck_id, "p1")
        assert _evaluated(control_loop_store, recheck_id, "p2")


@pytest.mark.asyncio
class TestBelowMinDtePreservedAcrossHourlyChecks:
    """Item 11: a held position below the candidate-entry min DTE still
    has its exact expiration retrieved on a later hourly recheck."""

    async def test_aged_below_min_dte_still_retrieved_on_recheck(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(environment, _position_at_dte("p1", "QQQ", 10, as_of=now_830))
        provider = _provider_for("QQQ", 10, reference_now=now_830)
        cycle = _load_script_module("_v1511_below_min_dte_hourly", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert _evaluated(control_loop_store, _lifecycle_cycle_id_for(now_1130), "p1")


@pytest.mark.asyncio
class TestMissingAndStaleContractsFailClosedOnRecheck:
    """Items 12, 13: a missing exact held contract, and a stale exact
    held contract, both still fail closed on an hourly recheck."""

    async def test_missing_exact_contract_fails_closed(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        # Only the short leg exists anywhere in this provider's data.
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices_on("SPY", _EDT_DATE), _held_slice_on("SPY", _EDT_DATE, 30, [(450.0, OptionRight.PUT)])]},
            reference_now=now_830,
        )
        cycle = _load_script_module("_v1511_missing_contract_hourly", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1130)
        assert not _evaluated(control_loop_store, recheck_id, "p1")

    async def test_stale_exact_contract_fails_closed(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={"SPY": [*_dense_near_term_slices_on("SPY", _EDT_DATE), _held_slice_on("SPY", _EDT_DATE, 30)]},
            stale_tickers=frozenset({"SPY"}), reference_now=now_830,
        )
        cycle = _load_script_module("_v1511_stale_contract_hourly", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1130)
        assert not _evaluated(control_loop_store, recheck_id, "p1")


@pytest.mark.asyncio
class TestProviderFailureIsolationOnRecheck:
    """Item 14: one ticker's provider failure is isolated from another
    ticker's success, preserved across hourly rechecks."""

    async def test_one_ticker_fails_another_healthy_on_recheck(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(
            environment,
            _position_at_dte("p1", "SPY", 30, as_of=now_830),
            _position_at_dte("p2", "QQQ", 30, as_of=now_830),
        )
        provider = _DenseExpirationFakeProvider(
            slices_by_ticker={
                "SPY": [*_dense_near_term_slices_on("SPY", _EDT_DATE), _held_slice_on("SPY", _EDT_DATE, 30)],
                "QQQ": [*_dense_near_term_slices_on("QQQ", _EDT_DATE), _held_slice_on("QQQ", _EDT_DATE, 30)],
            },
            raising_tickers=frozenset({"SPY"}), reference_now=now_830,
        )
        cycle = _load_script_module("_v1511_isolation_hourly", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        recheck_id = _lifecycle_cycle_id_for(now_1130)
        assert not _evaluated(control_loop_store, recheck_id, "p1")
        assert _evaluated(control_loop_store, recheck_id, "p2")


@pytest.mark.asyncio
class TestNoCandidatesFromHourlyPath:
    """Item 15: the lifecycle hourly path generates zero candidates,
    however many times it is invoked."""

    async def test_zero_candidates_across_two_hourly_checks(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        provider = _provider_for("SPY", 30, reference_now=now_830)
        cycle = _load_script_module("_v1511_zero_candidates", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        review_store = _review_store(ops)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == []


@pytest.mark.asyncio
class TestNoExecutionPathFromHourlyRecheck:
    """Item 16: no PaperBroker/order/fill/confirmation path is reachable
    from the lifecycle hourly recheck path, however many times invoked."""

    async def test_no_order_fill_or_confirmation_across_two_hourly_checks(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        now_1130 = _utc_for_eastern(_EDT_DATE, 11, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        provider = _provider_for("SPY", 30, reference_now=now_830)
        cycle = _load_script_module("_v1511_no_execution_path", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        await _run(cycle, provider, now_830)
        await _run(cycle, provider, now_1130)

        ops = _ops_config_module.load_operations_config()
        idempotency = _idempotency_store(ops)
        assert idempotency.all() == [], "the lifecycle-only path never constructs a PaperBroker, so it cannot place an order"

        portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
        portfolio = portfolio_store.get(COHORT_ID)
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0].position_id == "p1"


class TestDashboardHourlyRecheck:
    """Items 18, 19, 21: CLI and dashboard behavior parity for the
    hourly recheck, including that the existing dashboard asyncio lock
    plus bucket idempotency together prevent duplicate work on a
    repeated/simultaneous-style request."""

    def test_dashboard_post_performs_the_hourly_recheck_and_dedupes_a_repeat(self, environment, monkeypatch):
        import src.dashboard.validation_ops as validation_ops
        from fastapi.testclient import TestClient

        from src.dashboard.app import app

        _patch_env(monkeypatch)
        now_830 = _utc_for_eastern(_EDT_DATE, 8, 30)
        _seed_positions(environment, _position_at_dte("p1", "SPY", 30, as_of=now_830))
        validation_ops._runner_module = None
        runner_module = validation_ops._load_runner_module()
        provider = _provider_for("SPY", 30, reference_now=now_830)
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(runner_module, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        client = TestClient(app)
        resp1 = client.post("/api/validation-cycle/run")
        assert resp1.status_code == 200
        assert resp1.json()["success"] is True
        calls_after_first = len(provider.get_option_chain_for_dte_window_calls)

        # A repeat request (simulating a double-click) within the same
        # hour: the pre-existing dashboard asyncio.Lock serializes it,
        # and the (now hourly-bucketed) cycle-id idempotency check makes
        # it a documented no-op -- no second fetch.
        resp2 = client.post("/api/validation-cycle/run")
        assert resp2.status_code == 200
        assert resp2.json()["success"] is True
        assert len(provider.get_option_chain_for_dte_window_calls) == calls_after_first

        validation_ops._runner_module = None


@pytest.mark.asyncio
class TestRegressionOfPriorLifecycleSuites:
    """Item 22: a direct, targeted smoke re-check that V1.5.9's zero-
    position gate-closed safe no-op is still byte-for-byte unaffected --
    the full V1.5.9/V1.5.10 suites are re-run separately as part of this
    release's full-suite regression, not duplicated here line-for-line."""

    async def test_closed_gate_zero_positions_still_returns_false_and_persists_nothing(self, environment, monkeypatch):
        _patch_env(monkeypatch)
        now = _utc_for_eastern(_EDT_DATE, 8, 30)
        cycle = _load_script_module("_v1511_regression_smoke", SCRIPT_PATH)

        def _must_not_be_called():
            raise AssertionError("provider must never be constructed when the gate is closed and no positions exist")

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: _must_not_be_called())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_validation_cycle(now=now)
        assert ok is False

        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        assert control_loop_store.get_cycle_record(_main_cycle_id_for(now)) is None
        assert control_loop_store.get_cycle_record(_lifecycle_cycle_id_for(now)) is None
