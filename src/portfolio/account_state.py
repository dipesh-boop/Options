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
