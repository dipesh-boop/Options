"""Step 22.5 (PAPER_TRADING_V1.4.4): durable PaperBroker account state and
the domain `Portfolio` object across process restarts.

Two separate, independently-persisted pieces of state, because they always
have been two separate, independently-maintained objects in this codebase
(this module changes neither's own update logic, only adds durability):

- `PaperAccountState` mirrors exactly what `PaperBroker` itself tracks
  privately (cash, reserved collateral, its broker-side `Position` book,
  fills, rejection reasons, and the order-legs cache `attempt_fill` needs)
  -- see `src.brokers.paper.PaperBroker.export_state`/`restore_state`,
  the two new, purely additive methods this module's types round-trip
  through.
- The domain `src.risk.portfolio_risk.Portfolio` object (nav, cash,
  `PortfolioPosition` legs/strategy/capital_at_risk) is what the Risk
  Engine, Lifecycle Engine, and Portfolio Control Loop actually consume --
  built and updated by `src.orchestration.pipeline.default_portfolio_update_stage`,
  never derived from `PaperAccountState`.

Same ABC + InMemory + Sqlite triad every other durable store in this
codebase already uses (`src.portfolio.persistence.ControlLoopStore`,
`src.lifecycle.persistence.LifecycleStore`, and the validation package's
own equivalent store for cohort snapshots): each Sqlite operation opens
and closes its own
connection, so the store itself holds no in-process state a crash could
lose. Both stores are REPLACE-on-save keyed by `account_id` -- there is
exactly one current account state and one current Portfolio per account,
never a history of them (fills/decision snapshots elsewhere already carry
the append-only history).
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from src.brokers.base import Fill, OrderLeg, Position
from src.risk.portfolio_risk import Portfolio


@dataclass(frozen=True)
class PaperAccountState:
    """Exactly `PaperBroker`'s six private, account-level attributes
    (`_cash`, `_reserved_collateral`, `_positions`, `_fills`,
    `_rejection_reasons`, `_order_legs_cache`) -- deliberately NOT market
    data (`_chains`/`_underlyings`, re-fed every cycle by the caller via
    `update_market_data`) and NOT order idempotency (already durable via
    `src.brokers.base.SqliteIdempotencyStore`, which `PaperBroker` is
    constructed with directly)."""

    account_id: str
    cash: float
    reserved_collateral: float
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    rejection_reasons: dict[str, str] = field(default_factory=dict)
    order_legs_cache: dict[str, list[OrderLeg]] = field(default_factory=dict)
    saved_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


_PAPER_ACCOUNT_STATE_ADAPTER: TypeAdapter[PaperAccountState] = TypeAdapter(PaperAccountState)


class PaperAccountStateStore(ABC):
    @abstractmethod
    def save(self, state: PaperAccountState) -> None: ...

    @abstractmethod
    def get(self, account_id: str) -> PaperAccountState | None: ...


class InMemoryPaperAccountStateStore(PaperAccountStateStore):
    """Process-local only -- lost on restart. For tests and any caller
    that doesn't need restart-survival."""

    def __init__(self) -> None:
        self._states: dict[str, PaperAccountState] = {}

    def save(self, state: PaperAccountState) -> None:
        self._states[state.account_id] = state

    def get(self, account_id: str) -> PaperAccountState | None:
        return self._states.get(account_id)


class SqlitePaperAccountStateStore(PaperAccountStateStore):
    """A durable `PaperAccountStateStore` backed by a single sqlite file."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_account_state ("
                "account_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, state: PaperAccountState) -> None:
        state_json = _PAPER_ACCOUNT_STATE_ADAPTER.dump_json(state).decode("utf-8")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_account_state (account_id, state_json) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET state_json = excluded.state_json",
                (state.account_id, state_json),
            )

    def get(self, account_id: str) -> PaperAccountState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM paper_account_state WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        return _PAPER_ACCOUNT_STATE_ADAPTER.validate_json(row[0])


class PortfolioStore(ABC):
    @abstractmethod
    def save(self, account_id: str, portfolio: Portfolio) -> None: ...

    @abstractmethod
    def get(self, account_id: str) -> Portfolio | None: ...


class InMemoryPortfolioStore(PortfolioStore):
    """Process-local only -- lost on restart."""

    def __init__(self) -> None:
        self._portfolios: dict[str, Portfolio] = {}

    def save(self, account_id: str, portfolio: Portfolio) -> None:
        self._portfolios[account_id] = portfolio

    def get(self, account_id: str) -> Portfolio | None:
        return self._portfolios.get(account_id)


class SqlitePortfolioStore(PortfolioStore):
    """A durable `PortfolioStore` backed by a single sqlite file."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS account_portfolio ("
                "account_id TEXT PRIMARY KEY, portfolio_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, account_id: str, portfolio: Portfolio) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO account_portfolio (account_id, portfolio_json) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET portfolio_json = excluded.portfolio_json",
                (account_id, portfolio.model_dump_json()),
            )

    def get(self, account_id: str) -> Portfolio | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT portfolio_json FROM account_portfolio WHERE account_id = ?", (account_id,)
            ).fetchone()
        return Portfolio.model_validate_json(row[0]) if row is not None else None


class PortfolioLoadError(RuntimeError):
    """PAPER_TRADING_V1.5.13 correction: raised by `load_portfolio_read_only`
    for a genuinely abnormal condition -- corrupt `portfolio_json`, or a
    sqlite error that is neither "the file doesn't exist" nor "the table
    doesn't exist" (e.g. a locked or malformed database file). Deliberately
    distinct from that function's own `None` return (which means "no
    portfolio on record yet," a normal, expected outcome) -- a caller must
    never conflate the two, since silently treating corruption as "no
    portfolio" would mean a diagnostic (or anything else using this loader)
    quietly substitutes a fresh empty-NAV portfolio for a real one that
    exists but can't be read, which is exactly the failure mode this
    exception exists to prevent."""


def load_portfolio_read_only(db_path: Path | str, account_id: str) -> Portfolio | None:
    """A dedicated loader that is read-only by construction, not by
    caller discipline -- unlike `SqlitePortfolioStore.__init__` (which
    unconditionally runs `CREATE TABLE IF NOT EXISTS` and therefore CAN
    write a real schema change into a database file or directory that
    doesn't have it yet, confirmed by the PAPER_TRADING_V1.5.13 acceptance
    audit), this function never creates a directory, never creates a
    database file, never creates or alters a table, and never opens a
    connection capable of writing at all.

    It opens the database via SQLite's own `mode=ro` URI connection option
    (`sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)`) -- a mode
    SQLite enforces at the OS file-descriptor level (the file is opened
    O_RDONLY), not merely a Python-level convention a future change to this
    module could accidentally weaken. `PRAGMA query_only = ON` is set
    immediately after connecting as pure in-connection-memory defense in
    depth (it is a per-connection flag, not a database write -- verified
    empirically to leave the file byte-for-byte unchanged); `mode=ro`
    alone already makes any write attempt raise `sqlite3.OperationalError:
    attempt to write a readonly database` (also verified empirically), so
    this function would behave identically to the Risk/Quant pipeline even
    without it.

    Three outcomes:
    - `None`: no portfolio is on record for `account_id` yet -- either
      the database file doesn't exist, the `account_portfolio` table
      doesn't exist, or the table exists but has no row for this account.
      All three are normal, expected states for a never-before-used
      account, never created or migrated by this function.
    - a `Portfolio`: a row was found and its `portfolio_json` parses
      cleanly.
    - `PortfolioLoadError` raised: the database could not be read for a
      reason OTHER than "doesn't exist yet" (a genuinely abnormal sqlite
      error), or a row was found but its `portfolio_json` is corrupt --
      fails closed rather than silently returning `None` and letting a
      caller mistake real, unreadable account data for "no portfolio,"
      which would understate risk/exposure instead of refusing to guess."""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc):
            return None
        raise PortfolioLoadError(f"could not open {db_path!r} read-only: {exc}") from exc

    try:
        conn.execute("PRAGMA query_only = ON")
        try:
            row = conn.execute(
                "SELECT portfolio_json FROM account_portfolio WHERE account_id = ?", (account_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise PortfolioLoadError(f"unexpected sqlite error reading {db_path!r}: {exc}") from exc
    finally:
        conn.close()

    if row is None:
        return None
    try:
        return Portfolio.model_validate_json(row[0])
    except Exception as exc:  # noqa: BLE001 -- any parse/validation failure is corruption, fail closed
        raise PortfolioLoadError(
            f"corrupt portfolio_json for account {account_id!r} in {db_path!r}: {exc}"
        ) from exc
