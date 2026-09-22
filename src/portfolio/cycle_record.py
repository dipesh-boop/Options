"""Part 31: the immutable `ControlCycleRecord`.

One record per control-loop cycle (`src.portfolio.control_loop
.run_control_cycle`, Part 12) -- critical evidence for the eventual
90-day validation cohort (Part 41), and the first thing to check when
something looks wrong ("did cycle X even run, and what did it see").
Append-only, exactly like `PortfolioControlDecisionSnapshot`: a cycle
that fails partway through still gets a record (with `completed_at=None`
and whatever `errors` were captured), never silently vanishes.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class ControlCycleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None

    market_open: bool
    provider: str
    provider_health_status: str

    symbols_requested: tuple[str, ...] = ()
    symbols_successful: tuple[str, ...] = ()
    symbols_failed: tuple[str, ...] = ()

    positions_evaluated: int = 0
    lifecycle_triggers: int = 0
    risk_events: int = 0
    recommendations_created: int = 0

    opportunities_scanned: int = 0
    candidates_generated: int = 0
    candidates_rejected: int = 0

    requests_used: int | None = None
    rate_limit_available: int | None = None

    errors: tuple[str, ...] = ()
    degraded_mode: bool = False
    halt_state: bool = False

    _validate_started = field_validator("started_at")(_tz_aware)

    @field_validator("completed_at")
    @classmethod
    def _validate_completed(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    @property
    def had_errors(self) -> bool:
        return len(self.errors) > 0
