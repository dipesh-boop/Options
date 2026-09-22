"""Step 22.5 (PAPER_TRADING_V1.4.4): the production dashboard-startup
entry point. `scripts/start.sh` launches `src.dashboard.bootstrap:app`
instead of `src.dashboard.app:app` directly, so a real operator run of
`make run` can show the persisted control-loop/candidate-review state the
validation-cycle CLI has already produced, without any manual wiring step.

Imports and re-exports the real, unmodified FastAPI `app` object from
`src.dashboard.app` -- every route, every existing behavior, is exactly
as that module defines it; this file adds nothing beyond the one-time,
import-time store wiring below. A test harness that imports
`src.dashboard.app` directly (as every existing dashboard test already
does) is entirely unaffected by this module's existence.
"""
from __future__ import annotations

from src.dashboard.app import app, set_candidate_review_store  # noqa: F401
from src.portfolio.operations_config import OperationsConfigError, load_operations_config
from src.review.candidates import SqliteCandidateReviewStore

try:
    _ops = load_operations_config()
    set_candidate_review_store(SqliteCandidateReviewStore(_ops.candidate_review_db_path))
except OperationsConfigError:
    # No config/operations.yaml (or it's malformed) -- the dashboard still
    # starts, exactly as it always has; /api/candidates honestly reports
    # 503 (no store configured) rather than the whole app refusing to
    # start over an operational-layer config file the dashboard's other
    # routes don't need at all.
    pass
