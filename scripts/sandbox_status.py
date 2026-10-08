#!/usr/bin/env python3
"""PAPER_TRADING_V1.5.15: strictly read-only status command for the
Expanded-Universe Sandbox.

**If the sandbox database does not exist, this prints
`SANDBOX NOT INITIALIZED` and exits 0 -- without creating the DB file,
any directory, any SQLite schema, any WAL/SHM file, any cohort record,
any account state, or any other artifact.** `Path.exists()` is the
very first thing this script checks, before any config is loaded and
before any store class (every one of which runs `CREATE TABLE IF NOT
EXISTS` in its own `__init__`, which genuinely writes a missing
database/table into existence -- the PAPER_TRADING_V1.5.13 acceptance
audit already proved this for `SqlitePortfolioStore`, and every other
`Sqlite*Store` in this codebase shares the identical pattern) is ever
constructed.

**Every read, even once the database file exists, uses a `mode=ro`
SQLite URI connection.** This script never constructs
`SqliteValidationStore`/`SqliteCandidateReviewStore`/
`SqliteControlLoopStore`/`SqliteLifecycleStore`/`SqlitePortfolioStore`/
`SqlitePaperAccountStateStore` at all -- not even once the file exists
-- because each one's own `CREATE TABLE IF NOT EXISTS` DDL still opens
a WRITE-capable connection (and, in principle, could create a table
that doesn't yet exist, e.g. on a partially-initialized or
hand-truncated file), which this script must never risk regardless of
how unlikely that is in practice. Instead it opens ONE `mode=ro`
connection (the same technique `src.portfolio.account_state
.load_portfolio_read_only` already established -- enforced by SQLite
at the OS file-descriptor level, not by caller discipline) and queries
the exact same tables/columns those store classes themselves use,
parsing each row with the exact same public `from_jsonable`/
`model_validate_json`/`TypeAdapter` helpers they call internally. This
is why the acceptance tests for this script can assert the database
file's SHA-256 hash is byte-for-byte identical before and after every
invocation, mutation-free by construction rather than by convention.

**Never contacts Tradier.** Provider configuration is read directly
from `src.data.factory.DataProviderSelection` (a plain
`pydantic_settings.BaseSettings` environment-variable read) -- the
exact same env-only read `verify_official_provider_is_tradier_production`
performs internally, with no provider instance ever constructed and no
network call of any kind.

Usage:
    python scripts/sandbox_status.py

Exit codes: always 0 (a read-only status command has no failure mode
of its own beyond a genuinely systemic error, e.g. a corrupt database
file, which is reported and still exits 0 -- there is nothing to
retry or fix by re-running this script differently).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Section 1/27A: the official-database rejection gate -- imported and
# invoked BEFORE any other project import below has a chance to load a
# config file or touch a provider. Status is read-only, but the guard
# still applies here first, as it does in every sandbox entry point.
from src.portfolio.sandbox_guard import SandboxGuardError, assert_path_is_not_official_database  # noqa: E402
from src.portfolio.sandbox_identity import SANDBOX_DATABASE_PATH  # noqa: E402

try:
    assert_path_is_not_official_database(SANDBOX_DATABASE_PATH)
except SandboxGuardError as exc:
    print(f"FAIL: {exc}")
    raise SystemExit(0)

from src.data.factory import DataProviderSelection  # noqa: E402
from src.data.universe import UniverseConfigError, load_universe  # noqa: E402
from src.portfolio.account_state import PaperAccountState  # noqa: E402
from src.portfolio.cycle_record import ControlCycleRecord  # noqa: E402
from src.portfolio.sandbox_identity import (  # noqa: E402
    SANDBOX_ACCOUNT_ID,
    SANDBOX_COHORT_ID,
    SANDBOX_UNIVERSE_CONFIG_PATH,
)
from src.risk.portfolio_risk import Portfolio  # noqa: E402
from src.validation.records import CohortRecord, from_jsonable  # noqa: E402

from pydantic import TypeAdapter  # noqa: E402

_PAPER_ACCOUNT_STATE_ADAPTER: TypeAdapter[PaperAccountState] = TypeAdapter(PaperAccountState)

_BANNER_NOT_INITIALIZED = (
    "=================================================================\n"
    "PAPER_TRADING_V1.5.15 -- EXPANDED-UNIVERSE SANDBOX -- STATUS\n"
    "NOT OFFICIAL VALIDATION\n"
    "=================================================================\n"
    "\n"
    "SANDBOX NOT INITIALIZED\n"
    f"  ({SANDBOX_DATABASE_PATH} does not exist -- nothing has been created.)\n"
    "\n"
    "Run scripts/init_expanded_universe_sandbox.py to initialize it.\n"
    "This status command never creates the database itself."
)

_BANNER_HEADER = (
    "=================================================================\n"
    "PAPER_TRADING_V1.5.15 -- EXPANDED-UNIVERSE SANDBOX -- STATUS\n"
    "NOT OFFICIAL VALIDATION\n"
    "================================================================="
)


def _line(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def _readonly_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return row is not None


def main() -> int:
    if not SANDBOX_DATABASE_PATH.exists():
        print(_BANNER_NOT_INITIALIZED)
        return 0

    print(_BANNER_HEADER)
    print()
    _line("db path", str(SANDBOX_DATABASE_PATH))
    _line("cohort ID", SANDBOX_COHORT_ID)
    _line("account ID", SANDBOX_ACCOUNT_ID)

    conn = _readonly_connect(SANDBOX_DATABASE_PATH)
    try:
        cohort: CohortRecord | None = None
        if _table_exists(conn, "cohorts"):
            row = conn.execute("SELECT record_json FROM cohorts WHERE cohort_id = ?", (SANDBOX_COHORT_ID,)).fetchone()
            if row is not None:
                cohort = from_jsonable(json.loads(row[0]), CohortRecord)

        if cohort is None:
            print("\nSANDBOX DATABASE FILE EXISTS BUT NO COHORT RECORD WAS FOUND.")
            print("  Run scripts/init_expanded_universe_sandbox.py to initialize the cohort.")
            return 0

        _line("experiment version ID", cohort.manifest.experiment_version_id or "(none recorded)")
        _line("cohort status", cohort.status)
        _line("cohort start date", cohort.manifest.period.start_date.isoformat())
        _line("starting NAV", f"${cohort.manifest.starting_nav:,.2f}")

        portfolio: Portfolio | None = None
        if _table_exists(conn, "account_portfolio"):
            row = conn.execute(
                "SELECT portfolio_json FROM account_portfolio WHERE account_id = ?", (SANDBOX_ACCOUNT_ID,)
            ).fetchone()
            if row is not None:
                portfolio = Portfolio.model_validate_json(row[0])

        if portfolio is not None:
            _line("current NAV", f"${portfolio.nav:,.2f}")
            _line("cash", f"${portfolio.cash:,.2f}")
            _line("open positions", len(portfolio.positions))
        else:
            _line("current NAV / cash / open positions", "(no portfolio recorded yet)")

        completed_trades = 0
        if _table_exists(conn, "trades"):
            (completed_trades,) = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        _line("completed simulated trades", completed_trades)

        awaiting_human = 0
        if _table_exists(conn, "reviewed_candidates"):
            (awaiting_human,) = conn.execute(
                "SELECT COUNT(*) FROM reviewed_candidates WHERE status = 'awaiting_human' AND cohort_id = ?",
                (SANDBOX_COHORT_ID,),
            ).fetchone()
        _line("awaiting-human candidates", awaiting_human)

        latest_cycle: ControlCycleRecord | None = None
        if _table_exists(conn, "control_cycle_records"):
            row = conn.execute(
                "SELECT record_json FROM control_cycle_records ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                latest_cycle = ControlCycleRecord.model_validate_json(row[0])
        if latest_cycle is not None:
            _line(
                "latest sandbox cycle",
                f"{latest_cycle.cycle_id!r} (started {latest_cycle.started_at.isoformat()}, "
                f"positions_evaluated={latest_cycle.positions_evaluated}, "
                f"lifecycle_triggers={latest_cycle.lifecycle_triggers}, degraded_mode={latest_cycle.degraded_mode})",
            )
        else:
            _line("latest sandbox cycle", "(none recorded yet)")

        latest_lifecycle_observation = None
        if _table_exists(conn, "lifecycle_snapshots"):
            row = conn.execute(
                "SELECT trade_id, timestamp FROM lifecycle_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                latest_lifecycle_observation = f"trade_id={row[0]!r} at {row[1]}"
        _line("latest lifecycle observation", latest_lifecycle_observation or "(none recorded yet)")
    finally:
        conn.close()

    try:
        universe = load_universe(SANDBOX_UNIVERSE_CONFIG_PATH)
        _line("12-symbol universe", ", ".join(e.ticker for e in universe))
    except UniverseConfigError as exc:
        _line("universe", f"could not load -- {exc}")

    provider = DataProviderSelection()
    _line("provider configuration (not contacted)", provider.data_provider)

    print()
    print("PAPERBROKER SIMULATION ONLY. NO LIVE EXECUTION.")
    print("This status command is strictly read-only -- it never mutated any sandbox state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
