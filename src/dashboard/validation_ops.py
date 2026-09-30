"""Step 22.8 (PAPER_TRADING_V1.4.7): read-only operator status
aggregation, plus the one safe, explicit dashboard action that may
trigger the daily validation cycle.

**Why this is safe.** `trigger_validation_cycle` below does not
reimplement any part of the daily cycle's own logic -- it loads
`scripts/run_validation_cycle.py` as a module (the exact same technique
`tests/acceptance/test_review_only_daily_cycle.py`'s own
`_load_script_module` helper already uses to exercise that script
in-process) and awaits its unmodified `run_validation_cycle()`
coroutine directly. Every safety guarantee that function already has --
the Tradier-production-provider preflight before any mutation,
cycle-level idempotency (a second call the same day is a documented
no-op), Review-Only new-position handling (it never calls
`PaperBroker.place_order`) -- therefore applies here identically. This
module adds no new mutation path of its own: it is a thin, read-only-
except-for-this-one-call wrapper, never a reimplementation.

Nothing here can confirm a candidate. `src.review.confirmation
.confirm_candidate` is not imported anywhere in this file, and no
route built on top of it accepts a candidate id -- confirming a
Review-Only candidate stays exactly what it already was: a separate,
deliberate `scripts/confirm_candidate.py` operator command.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel, ConfigDict

from src.dashboard import schemas
from src.data.factory import (
    DataProviderSelection,
    OfficialProviderPreflightError,
    verify_official_provider_is_tradier_production,
)
from src.portfolio.market_session import evaluate_validation_cycle_eligibility
from src.portfolio.operations_config import OperationsConfigError, load_operations_config
from src.portfolio.persistence import SqliteControlLoopStore
from src.review.candidates import SqliteCandidateReviewStore
from src.validation.freeze import FREEZE_NAME
from src.validation.protocol import ValidationConfigError, load_validation_config
from src.validation.session import SqliteValidationStore

REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_validation_cycle.py"
_RUNNER_MODULE_NAME = "_dashboard_validation_cycle_runner"


# ------------------------------------------------------------- status


class ProviderReadinessView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    is_tradier_production: bool
    ready: bool
    detail: str


class OperatorStatusView(BaseModel):
    """Everything Step 22.8's "simple daily operator experience" asks
    the dashboard to surface, in one read-only response. Never carries
    a secret of any kind -- `detail`/`provider.detail` come from
    `OfficialProviderPreflightError`'s own message, which is written to
    never interpolate a token (see that exception's raise sites in
    `src.data.factory`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # PAPER_TRADING_V1.5.5, Step 4: backend-derived, so the dashboard
    # never needs its own hard-coded version string again -- sourced
    # directly from `src.validation.freeze.FREEZE_NAME` (the same
    # constant `make verify-freeze`/the freeze report already treat as
    # this codebase's single source of truth for "what version is
    # this"), a fixed Python default so it is present even in the
    # `configured=False` degraded branch below. Purely presentational:
    # nothing reads this field to make a trading decision.
    software_version: str = FREEZE_NAME

    configured: bool
    detail: str | None = None

    cohort_id: str | None = None
    cohort_started_at: date | None = None
    cohort_planned_end_date: date | None = None

    nav: float | None = None
    cash: float | None = None
    open_position_count: int | None = None
    drawdown_pct: float | None = None
    last_snapshot_date: str | None = None

    today_cycle_id: str | None = None
    today_cycle_ran: bool = False
    today_cycle_degraded: bool | None = None
    today_cycle_halted: bool | None = None
    today_cycle_errors: tuple[str, ...] = ()

    provider: ProviderReadinessView

    awaiting_review_count: int = 0
    awaiting_review_candidate_ids: tuple[str, ...] = ()

    validation_duration_days: int | None = None
    validation_days_recorded: int = 0
    validation_minimum_completed_trades: int | None = None
    validation_preferred_completed_trades: int | None = None
    validation_completed_trades: int = 0

    alerts: tuple[schemas.ControlLoopAlertView, ...] = ()

    # PAPER_TRADING_V1.5.1, Step 2: the new-position daily opportunity
    # scan's market-hours safety gate, surfaced read-only so the
    # dashboard can render backend-authoritative session state and
    # disable the Run button without ever calculating market hours
    # itself. `None`/`False`-defaulted (never a fabricated "open") in
    # the `configured=False` degraded branch below, matching every
    # other field in this view.
    market_session_state: str | None = None
    is_trading_day: bool | None = None
    regular_session_open: datetime | None = None
    regular_session_close: datetime | None = None
    validation_cycle_allowed: bool = False
    validation_cycle_block_reason: str | None = None

    # PAPER_TRADING_V1.5.3, Step 3: read-only observability for the
    # sector/correlation risk-data wiring capability
    # (src.portfolio.risk_data) -- "INSTALLED_ACTIVE" once a cohort's
    # own operator config has explicitly turned it on, "INSTALLED_INACTIVE"
    # otherwise (the active cohort's own default). `None` only in the
    # `configured=False` degraded branch, matching every other field
    # here. This is observability only -- there is no control here to
    # change it; that stays a config-file edit, never a dashboard action.
    risk_data_wiring_status: str | None = None

    # PAPER_TRADING_V1.5.5, Step 4: read-only projection of today's
    # `src.workflows.candidate_funnel.CandidateFunnel`, if today's cycle
    # ran with collection enabled (see `scripts/run_validation_cycle.py`'s
    # `collect_candidate_funnel=True`). `None`/`()` whenever no cycle has
    # run yet today or that cycle predates this field -- never fabricated,
    # matching every other `today_cycle_*` field above. Deliberately a
    # SUBSET of `CandidateFunnel`'s fields (the ones a "what happened and
    # why" glance needs), never the full per-symbol/per-strategy detail --
    # this is presentational summary, not a raw-debugging dump, and (like
    # every field in this view) purely observational: nothing here can be
    # used to confirm a candidate, change a threshold, or trigger a trade.
    candidate_funnel_symbols_scanned: int | None = None
    candidate_funnel_chains_usable: int | None = None
    candidate_funnel_contracts_seen: int | None = None
    candidate_funnel_strategy_attempts: int | None = None
    candidate_funnel_construction_successes: int | None = None
    candidate_funnel_quant_rejected: int | None = None
    candidate_funnel_risk_rejected: int | None = None
    candidate_funnel_candidates_persisted: int | None = None
    candidate_funnel_top_bottlenecks: tuple[str, ...] = ()
    candidate_funnel_zero_candidate_summary: str | None = None


def _provider_readiness() -> ProviderReadinessView:
    selection = DataProviderSelection()
    provider = selection.data_provider.lower()
    try:
        verify_official_provider_is_tradier_production(selection)
    except OfficialProviderPreflightError as exc:
        return ProviderReadinessView(provider=provider, is_tradier_production=False, ready=False, detail=str(exc))
    return ProviderReadinessView(
        provider=provider, is_tradier_production=True, ready=True,
        detail="Tradier production market data configured -- ready for an official cycle.",
    )


def build_operator_status(*, now: datetime | None = None) -> OperatorStatusView:
    """Pure read: opens each store's own connection, reads, closes --
    never creates a cohort, never writes a snapshot/cycle/candidate
    record, never touches PaperBroker. Degrades honestly (`configured=
    False`) rather than raising when `config/operations.yaml`/
    `config/validation.yaml` aren't available, matching
    `src.dashboard.bootstrap`'s own established "the dashboard still
    starts" tolerance for a missing operational-layer config file."""
    now = now or datetime.now(timezone.utc)
    provider_view = _provider_readiness()

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
    except (OperationsConfigError, ValidationConfigError) as exc:
        return OperatorStatusView(configured=False, detail=f"configuration not available: {exc}", provider=provider_view)

    validation_store = SqliteValidationStore(val_config.db_path)
    snapshots = sorted(validation_store.snapshots(cohort_id=ops.cohort_id), key=lambda s: s.snapshot_date)
    trades = validation_store.trades(cohort_id=ops.cohort_id)
    latest = snapshots[-1] if snapshots else None

    control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
    today_cycle_id = f"validation-{now.date().isoformat()}"
    cycle_record = control_loop_store.get_cycle_record(today_cycle_id)
    alerts = tuple(
        schemas.build_control_loop_alert_view(a) for a in control_loop_store.all_unresolved_alerts()
    )

    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    awaiting = review_store.candidates_awaiting_human(cohort_id=ops.cohort_id)

    cohort_record = validation_store.get_cohort(ops.cohort_id)
    cohort_start = _cohort_start_date(cohort_record, snapshots)
    planned_end = cohort_start + timedelta(days=val_config.duration_days) if cohort_start is not None else None

    eligibility = evaluate_validation_cycle_eligibility(
        now, scan_open_buffer_minutes=ops.scan_open_buffer_minutes, scan_close_buffer_minutes=ops.scan_close_buffer_minutes,
    )

    funnel = cycle_record.candidate_funnel if cycle_record is not None else None

    return OperatorStatusView(
        configured=True,
        cohort_id=ops.cohort_id,
        cohort_started_at=cohort_start,
        cohort_planned_end_date=planned_end,
        nav=latest.nav if latest else None,
        cash=latest.cash if latest else None,
        open_position_count=latest.open_position_count if latest else None,
        drawdown_pct=latest.drawdown_pct if latest else None,
        last_snapshot_date=latest.snapshot_date.isoformat() if latest else None,
        today_cycle_id=today_cycle_id,
        today_cycle_ran=cycle_record is not None,
        today_cycle_degraded=cycle_record.degraded_mode if cycle_record else None,
        today_cycle_halted=cycle_record.halt_state if cycle_record else None,
        today_cycle_errors=cycle_record.errors if cycle_record else (),
        provider=provider_view,
        awaiting_review_count=len(awaiting),
        awaiting_review_candidate_ids=tuple(c.candidate_id for c in awaiting),
        validation_duration_days=val_config.duration_days,
        validation_days_recorded=len(snapshots),
        validation_minimum_completed_trades=val_config.minimum_completed_trades,
        validation_preferred_completed_trades=val_config.preferred_completed_trades,
        validation_completed_trades=len(trades),
        alerts=alerts,
        market_session_state=eligibility.market_session_state.value,
        is_trading_day=eligibility.is_trading_day,
        regular_session_open=eligibility.regular_session_open,
        regular_session_close=eligibility.regular_session_close,
        validation_cycle_allowed=eligibility.validation_cycle_allowed,
        validation_cycle_block_reason=eligibility.block_reason,
        risk_data_wiring_status="INSTALLED_ACTIVE" if ops.risk_data_wiring_enabled else "INSTALLED_INACTIVE",
        candidate_funnel_symbols_scanned=funnel.symbols_requested if funnel else None,
        candidate_funnel_chains_usable=funnel.option_chains_quality_passed if funnel else None,
        candidate_funnel_contracts_seen=funnel.contracts_seen if funnel else None,
        candidate_funnel_strategy_attempts=funnel.strategy_attempts if funnel else None,
        candidate_funnel_construction_successes=funnel.construction_successes if funnel else None,
        candidate_funnel_quant_rejected=funnel.quant_rejected if funnel else None,
        candidate_funnel_risk_rejected=funnel.risk_rejected if funnel else None,
        candidate_funnel_candidates_persisted=funnel.candidates_persisted_for_review if funnel else None,
        candidate_funnel_top_bottlenecks=funnel.top_bottlenecks if funnel else (),
        candidate_funnel_zero_candidate_summary=funnel.zero_candidate_summary if funnel else None,
    )


def _cohort_start_date(cohort_record, snapshots) -> date | None:
    """The cohort's real start date, never fabricated: prefers the
    registered `CohortRecord.started_at` (the same record
    `src.validation.cohort.start_new_cohort` writes), and falls back to
    the earliest recorded `DailySnapshot.snapshot_date` only when no
    `CohortRecord` row exists for this `cohort_id` -- both are real,
    already-persisted facts, never a value this function invents."""
    if cohort_record is not None and cohort_record.started_at is not None:
        return cohort_record.started_at.date()
    if snapshots:
        return snapshots[0].snapshot_date
    return None


# ------------------------------------------------------------ trigger


class ValidationCycleRunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    cycle_id: str
    log: str


_runner_module: ModuleType | None = None
_trigger_lock = asyncio.Lock()


def _load_runner_module() -> ModuleType:
    """Cached after first use -- `scripts/run_validation_cycle.py`'s
    own top-level code is nothing but imports and definitions (no
    side effect fires unless `main()` runs, which only happens under
    `if __name__ == "__main__":`, never true for a module loaded under
    a different name like this)."""
    global _runner_module
    if _runner_module is None:
        spec = importlib.util.spec_from_file_location(_RUNNER_MODULE_NAME, _RUNNER_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RUNNER_MODULE_NAME] = module
        spec.loader.exec_module(module)
        _runner_module = module
    return _runner_module


async def trigger_validation_cycle() -> ValidationCycleRunView:
    """The one dashboard action that may run the official validation
    cycle. Serialized by a module-level lock so two near-simultaneous
    clicks from this dashboard can't race each other into overlapping
    in-process calls -- the cycle's own cycle-level idempotency
    (`control_loop_store.get_cycle_record(cycle_id)` checked before any
    mutation) already makes a same-day second run a documented no-op
    regardless, exactly as it already was for two terminal invocations
    of `make validate-cycle`; this lock only removes the redundant
    concurrent work, it does not change that pre-existing safety
    property."""
    now = datetime.now(timezone.utc)
    cycle_id = f"validation-{now.date().isoformat()}"
    async with _trigger_lock:
        module = _load_runner_module()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                success = await module.run_validation_cycle()
            except Exception as exc:  # noqa: BLE001 -- surfaced to the operator, never swallowed
                print(f"FAIL: validation cycle could not complete -- {exc!r}")
                success = False
        return ValidationCycleRunView(success=success, cycle_id=cycle_id, log=buffer.getvalue())
