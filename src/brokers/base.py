"""Abstract Broker interface plus the canonical trading-domain schemas
(Account, Position, Order, Fill) every concrete broker adapter must
normalize its raw responses into. See package docstring for the
layering rule this module holds to.

Order-mutating methods (`place_order`, `cancel_order`) are deliberately
NOT wrapped in automatic retry anywhere in this codebase — see
`Broker.place_order`'s docstring. `PlaceOrderRequest.client_order_id` is
the idempotency key that makes a caller-initiated retry safe instead of
dangerous; `IdempotencyStore` is the mechanism a concrete adapter uses
to honor it.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from pydantic import Field

from src.data.option_chain import OptionChain, OptionRight
from src.data.provider import StrictModel, TimestampedModel
from src.data.quotes import UnderlyingQuote


class BrokerEnvironment(str, Enum):
    """Deliberately a single-member enum. Adding LIVE later is a real
    code change to this type (and to every concrete adapter's
    connection-validation logic), never a config value someone can flip
    — the same principle ARCHITECTURE.md §4 applies to `TradingMode`."""

    PAPER = "paper"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    # Market orders are deliberately not supported. This platform's
    # strategies are defined-risk, price-controlled entries — a market
    # order surrenders exactly the price control that matters here.
    # Extending this enum is a future, deliberate decision, not an
    # oversight.


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


_TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})


class OrderLeg(StrictModel):
    """One leg of an order. A single-leg order (cash-secured put,
    covered call) has exactly one; a put credit spread has two,
    submitted as a combo order by the concrete adapter. `right`/`strike`/
    `expiration` are None for an equity leg."""

    symbol: str = Field(min_length=1, max_length=64)
    right: OptionRight | None = None
    strike: float | None = Field(default=None, gt=0)
    expiration: date | None = None
    action: OrderAction
    quantity: int = Field(gt=0)


class PlaceOrderRequest(StrictModel):
    """`client_order_id` is the idempotency key: the caller (not the
    broker) generates it once per intended order and reuses the exact
    same value on any retry. See `Broker.place_order`."""

    client_order_id: str = Field(min_length=1, max_length=64)
    legs: list[OrderLeg] = Field(min_length=1, max_length=4)
    order_type: OrderType = OrderType.LIMIT
    limit_price: float = Field(gt=0)
    time_in_force: str = Field(default="DAY", min_length=1, max_length=16)


class Order(TimestampedModel):
    client_order_id: str = Field(min_length=1, max_length=64)
    broker_order_id: str | None = None
    legs: list[OrderLeg]
    order_type: OrderType
    limit_price: float = Field(gt=0)
    status: OrderStatus
    filled_quantity: int = Field(default=0, ge=0)
    avg_fill_price: float | None = Field(default=None, ge=0)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class Fill(TimestampedModel):
    broker_order_id: str = Field(min_length=1, max_length=64)
    client_order_id: str | None = None
    symbol: str = Field(min_length=1, max_length=64)
    action: OrderAction
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    commission: float = Field(ge=0)


class Position(TimestampedModel):
    symbol: str = Field(min_length=1, max_length=64)
    quantity: int  # signed: positive = long, negative = short
    avg_cost: float = Field(ge=0)
    market_price: float = Field(ge=0)
    market_value: float
    unrealized_pnl: float


class Account(TimestampedModel):
    account_id: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=1, max_length=8)
    net_liquidation: float = Field(ge=0)
    buying_power: float = Field(ge=0)
    cash_balance: float
    maintenance_margin: float = Field(ge=0)


class ConnectionHealth(StrictModel):
    connected: bool
    environment: BrokerEnvironment
    account_id: str | None
    checked_at: datetime
    latency_ms: float | None = Field(default=None, ge=0)


class ReconciliationDiscrepancy(StrictModel):
    kind: str  # "orphaned_local" | "unknown_broker" | "status_mismatch"
    client_order_id: str | None
    broker_order_id: str | None
    detail: str = Field(min_length=1, max_length=500)


class ReconciliationReport(StrictModel):
    checked_at: datetime
    discrepancies: list[ReconciliationDiscrepancy] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies


class BrokerConnectionError(RuntimeError):
    """Raised when a broker adapter can't reach (or has lost its
    connection to) the broker. Read methods may retry after this; order
    mutation methods never do (see `Broker.place_order`)."""


class LiveTradingBlockedError(RuntimeError):
    """Raised when a broker adapter's resolved configuration is anything
    other than a known paper-trading endpoint. This is the concrete
    "do not allow live trading" mechanism — connection simply refuses
    to proceed, it doesn't warn and continue."""


class DuplicateOrderError(RuntimeError):
    """Raised only if an internal invariant is violated: the same
    client_order_id resolves to two different broker-side orders. This
    should never happen if `place_order`'s idempotency check is correct
    — it's a loud failure for a bug class that must never be silently
    papered over, not an expected error path."""


class IdempotencyStore(ABC):
    """Tracks client_order_id -> Order so `place_order` can detect and
    refuse to duplicate a submission. A concrete adapter is handed one
    of these (defaulting to `InMemoryIdempotencyStore`); Phase 0's real,
    persisted order state machine (ARCHITECTURE.md §7) is the eventual
    full durable implementation — `SqliteIdempotencyStore` below is a
    genuinely durable (survives a process restart), narrower interim
    implementation of this same interface, usable today wherever
    crash-then-retry safety actually matters."""

    @abstractmethod
    def get(self, client_order_id: str) -> Order | None: ...

    @abstractmethod
    def save(self, order: Order) -> None: ...

    @abstractmethod
    def all(self) -> list[Order]: ...


class InMemoryIdempotencyStore(IdempotencyStore):
    """Process-local only — lost on restart. A placeholder, not a
    production guarantee; flagged, not hidden. Use
    `SqliteIdempotencyStore` wherever a retry after a crash must still
    be recognized as a duplicate."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def get(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def save(self, order: Order) -> None:
        self._orders[order.client_order_id] = order

    def all(self) -> list[Order]:
        return list(self._orders.values())


class SqliteIdempotencyStore(IdempotencyStore):
    """SY-002 fix: a durable `IdempotencyStore` backed by a single
    sqlite file. Unlike `InMemoryIdempotencyStore`, a `client_order_id`
    recorded here is still recognized by a *freshly constructed*
    instance pointed at the same file — i.e. it survives the process
    that recorded it crashing and being restarted, which is exactly the
    guarantee `Broker.place_order`'s own docstring says a caller-side
    retry depends on. Each operation opens and closes its own
    connection rather than holding one open for the store's lifetime,
    so it has no in-process state that a "crash" (simply discarding the
    Python object, as this class's own tests do) could lose."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS idempotency_orders ("
                "client_order_id TEXT PRIMARY KEY, order_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def get(self, client_order_id: str) -> Order | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT order_json FROM idempotency_orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return Order.model_validate_json(row[0]) if row is not None else None

    def save(self, order: Order) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_orders (client_order_id, order_json) VALUES (?, ?)",
                (order.client_order_id, order.model_dump_json()),
            )

    def all(self) -> list[Order]:
        with self._connect() as conn:
            rows = conn.execute("SELECT order_json FROM idempotency_orders").fetchall()
        return [Order.model_validate_json(row[0]) for row in rows]


class Broker(ABC):
    """Every concrete broker adapter (IBKR today; Schwab, a future
    PaperBroker/BacktestBroker later) implements this. Every method's
    return type is a canonical schema — never a raw broker SDK object."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def health_check(self) -> ConnectionHealth: ...

    @abstractmethod
    async def get_account(self) -> Account: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote: ...

    @abstractmethod
    async def get_option_chain(self, symbol: str) -> OptionChain: ...

    @abstractmethod
    async def place_order(self, request: PlaceOrderRequest) -> Order:
        """Submit a paper order. Implementations must NEVER wrap this in
        automatic retry: if the call fails or times out, the caller
        genuinely doesn't know whether the broker received it, and a
        blind resubmission risks a duplicate order. The safe pattern is:
        the caller decides whether to retry, and if it does, it calls
        this method again with the *same* `request.client_order_id` —
        which every implementation must honor as an idempotency key
        (via `IdempotencyStore`), making that retry safe rather than
        dangerous."""
        ...

    @abstractmethod
    async def cancel_order(self, client_order_id: str) -> Order:
        """Also never auto-retried, for the same reason as
        `place_order` — an uncertain cancel result is exactly as
        dangerous to blindly retry as an uncertain submit."""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def get_fills(self, since: datetime | None = None) -> list[Fill]: ...

    @abstractmethod
    async def reconcile(self) -> ReconciliationReport:
        """Compare local order records against the broker's actual
        state and report discrepancies — orders we have locally the
        broker doesn't know about, orders the broker has that we don't,
        status mismatches. Never silently fixes anything; that's a
        decision for whatever calls this."""
        ...
