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
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel, ConfigDict

from src.data.factory import (
    DataProviderSelection,
    OfficialProviderPreflightError,
    verify_official_provider_is_tradier_production,
)
from src.portfolio.operations_config import OperationsConfigError, load_operations_config
from src.portfolio.persistence import SqliteControlLoopStore
from src.review.candidates import SqliteCandidateReviewStore
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

    configured: bool
    detail: str | None = None

    cohort_id: str | None = None

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
    validation_completed_trades: int = 0


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

    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    awaiting = review_store.candidates_awaiting_human(cohort_id=ops.cohort_id)

    return OperatorStatusView(
        configured=True,
        cohort_id=ops.cohort_id,
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
        validation_completed_trades=len(trades),
    )


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
