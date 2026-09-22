"""Step 22.4A Part 5: the explicit, production-safe projection from
persisted Portfolio Control Loop output into `DashboardState`.

Before this module, `DashboardState.latest_cycle_record`/`latest_exposure`/
`control_loop_alerts` were declared (Step 22.4, Parts 33-34) but nothing
anywhere in the repository ever actually populated them from
`src.portfolio.persistence.ControlLoopStore` -- so every dashboard
session showed them as permanently empty, and the dashboard's own
`/api/control-loop/*` routes (which honestly 404 on an empty state) had
no real path to ever return real data. This module is that one missing
projection: a pure, read-only reload of whatever the control loop has
already computed and persisted, never a recomputation of any of it.

**Never fabricates.** If no cycle has ever run, `state.latest_cycle_record`
stays `None` exactly as `DashboardState`'s own default already is --
this function never invents a placeholder cycle, exposure snapshot, or
alert. If a cycle exists but its exposure snapshot was never separately
saved (e.g. an older cycle recorded before `src.portfolio.orchestrator`
started persisting exposure), `latest_exposure` stays `None` rather than
guessing.
"""
from __future__ import annotations

from src.dashboard.models import DashboardState
from src.portfolio.persistence import ControlLoopStore


def load_latest_control_loop_state(state: DashboardState, *, control_loop_store: ControlLoopStore) -> DashboardState:
    """Reloads the most recently completed persisted cycle (plus its
    exposure snapshot and every currently-unresolved alert) into `state`,
    in place, and returns it for convenient chaining. Idempotent and
    side-effect-free on `control_loop_store` itself -- purely a read."""
    recent = control_loop_store.recent_cycle_records(limit=1)
    latest = recent[0] if recent else None
    state.latest_cycle_record = latest
    state.latest_exposure = control_loop_store.get_exposure_snapshot(latest.cycle_id) if latest is not None else None
    state.control_loop_alerts = {a.alert_id: a for a in control_loop_store.all_unresolved_alerts()}
    return state
