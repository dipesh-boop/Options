"""PAPER_TRADING_V1.5.15: the Expanded-Universe Sandbox's own, hardwired
identity -- cohort, account, database path, universe config path, and
manifest/experiment labels.

**Deliberately NOT a YAML config file.** This identity must never
depend on an operator-editable file that could be accidentally pointed
at the official database by a typo, a stray environment variable, or a
forgotten `export`. Every sandbox script imports these constants
directly from Python code -- there is no config file an operator could
edit (correctly or incorrectly) to make a sandbox script resolve to the
official database.

**Operational tuning knobs are deliberately NOT duplicated here.**
Confirmation TTL, price/capital drift tolerances, market-hours scan
buffers, and `risk_data_wiring` settings are genuine, substantive
PRODUCTION RESEARCH RULES this sandbox must share byte-for-byte with
the official cohort (PAPER_TRADING_V1.5.15's own mission statement:
"the same Quant, Risk, DTE, liquidity, sizing, ranking and no-trade
standards") -- see `load_sandbox_operations_config` below, which reads
them from the SAME `config/operations.yaml` the official cycle reads
(read-only; nothing in this module or its callers ever opens that file
for writing) rather than hand-maintaining a second copy that could
silently drift from the production values over time. Only identity
(cohort/account/database path) is overridden.

**Self-validating at import time.** `SANDBOX_DATABASE_PATH` is checked
against `src.portfolio.sandbox_guard` the moment this module is first
imported -- a future edit that accidentally reintroduced the official
path here fails with an `ImportError`-wrapped `SandboxGuardError` the
first time anything imports this module, never silently at the moment
a store is finally constructed."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from src.portfolio.operations_config import OperationsConfig, load_operations_config
from src.portfolio.sandbox_guard import assert_path_is_not_official_database

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------
# Identity (Section 2 of the PAPER_TRADING_V1.5.15 task specification)
# ---------------------------------------------------------------------

SANDBOX_DATABASE_PATH: Path = _REPO_ROOT / "data" / "options_agent_sandbox.db"
SANDBOX_UNIVERSE_CONFIG_PATH: Path = _REPO_ROOT / "config" / "universe_sandbox.yaml"

SANDBOX_COHORT_ID = "paper-trading-v1.5.15-expanded-universe-sandbox-2026-10-08"
SANDBOX_COHORT_NAME = "PAPER_TRADING_V1.5.15 Expanded-Universe Sandbox"
SANDBOX_ACCOUNT_ID = "paper-trading-v1.5.15-expanded-universe-sandbox"
SANDBOX_MANIFEST_ID = "paper-trading-v1.5.15-expanded-universe-sandbox-manifest"
SANDBOX_COHORT_LABEL = "EXPANDED_UNIVERSE_SANDBOX_V1"
SANDBOX_SOFTWARE_FREEZE_VERSION = "PAPER_TRADING_V1.5.15"

SANDBOX_START_DATE: date = date(2026, 10, 8)
SANDBOX_DURATION_DAYS = 90
SANDBOX_STARTING_NAV = 100_000.0

# PAPER_TRADING_V1.5.15: the sandbox cycle's own cycle-id namespace --
# see `src.portfolio.cycle_helpers.run_lifecycle_only_safety_check`'s
# own `cycle_id_prefix` parameter. Never `"validation"` (the official
# cycle's own, unparameterized prefix) -- a sandbox cycle id can
# therefore never collide with, consume, or be mistaken for an
# official `ControlCycleRecord`'s idempotency key, and vice versa.
SANDBOX_CYCLE_ID_PREFIX = "sandbox-validation"

# Fails closed at import time -- see this module's own docstring.
assert_path_is_not_official_database(SANDBOX_DATABASE_PATH)


def load_sandbox_operations_config() -> OperationsConfig:
    """Reads `config/operations.yaml` -- the SAME file the official
    cycle reads, read-only, never mutated here -- for every OPERATIONAL
    tuning knob (confirmation TTL, price/capital drift tolerances,
    market-hours scan buffers, `risk_data_wiring` settings, correlation
    parameters, default market regime), then overrides ONLY cohort/
    account identity and every durable-store path with the sandbox's
    own values. Every overridden database path is re-validated against
    `src.portfolio.sandbox_guard` before this function returns -- a
    defense-in-depth check on top of `SANDBOX_DATABASE_PATH`'s own
    import-time assertion above, in case a future edit to this
    function's own `update=` mapping ever introduced a mistake."""
    official = load_operations_config()
    db_path = str(SANDBOX_DATABASE_PATH)
    sandbox = official.model_copy(update={
        "cohort_id": SANDBOX_COHORT_ID,
        "account_id": SANDBOX_ACCOUNT_ID,
        "account_state_db_path": db_path,
        "control_loop_db_path": db_path,
        "lifecycle_db_path": db_path,
        "candidate_review_db_path": db_path,
    })
    for path in (
        sandbox.account_state_db_path, sandbox.control_loop_db_path,
        sandbox.lifecycle_db_path, sandbox.candidate_review_db_path,
    ):
        assert_path_is_not_official_database(path)
    return sandbox
