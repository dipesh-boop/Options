"""Interactive Brokers integration behind the `Broker` interface.

Paper accounts only. Connection is refused (`LiveTradingBlockedError`)
unless the configured port is a known IBKR *paper*-trading port, and
again, as a second, independent check, unless the account(s) IBKR
reports after connecting use the conventional paper-account prefix
("DU"). Neither check is a warning — both raise and abort the
connection.

All configuration (host, port, client id, expected account prefix)
comes from environment variables (see `IBKRConfig`) — nothing here is
hardcoded, and IBKR's API itself needs no credential over the wire (it
authenticates against an already-logged-in local TWS/IB Gateway
session), so there is no secret to accidentally embed in source in the
first place.

This module talks to IBKR only through `IBClientLike`, a small Protocol
this module defines itself — not `ib_insync`'s full API surface. The
real `ib_insync.IB` client is wrapped by `_RealIBAdapter`, isolating
every place this code depends on `ib_insync`'s actual method
names/return shapes into one reviewable class. Tests inject a fake
implementation of `IBClientLike` directly and never touch a real
connection or `ib_insync` at all.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timezone
from typing import Any, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.brokers.base import (
    Account,
    Broker,
    BrokerConnectionError,
    BrokerEnvironment,
    ConnectionHealth,
    DuplicateOrderError,
    Fill,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    LiveTradingBlockedError,
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    PlaceOrderRequest,
    Position,
    ReconciliationDiscrepancy,
    ReconciliationReport,
)
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote

SOURCE_IBKR = "ibkr"

# IBKR's well-known default ports. TWS paper=7497/live=7496, IB Gateway
# paper=4002/live=4001. An operator using a nonstandard port mapping
# must still resolve to one of these paper values, or connection is
# refused — this is deliberately not configurable to "trust me" a
# different port is paper.
PAPER_PORTS = frozenset({7497, 4002})
LIVE_PORTS = frozenset({7496, 4001})

# IBKR paper-trading account ids conventionally start with "DU"
# (live accounts typically start with "U"). This is a convention, not a
# guarantee IBKR will never change — kept as a second, independent
# check alongside the port check, not the sole line of defense.
DEFAULT_PAPER_ACCOUNT_PREFIX = "DU"

_READ_RETRY_ATTEMPTS = 3
_READ_RETRY_BASE_DELAY_SECONDS = 0.05


class IBKRConfig(BaseSettings):
    # Step 22.8: env_ignore_empty=True -- see DataProviderSelection's
    # comment in src/data/factory.py for why this matters given the
    # shipped .env template's intentionally-blank optional variables.
    model_config = SettingsConfigDict(env_prefix="OPTIONS_AGENT_IBKR_", env_ignore_empty=True)

    host: str = "127.0.0.1"
    port: int = 7497  # TWS paper by default — the safer default of the two paper ports
    client_id: int = 7
    account_id: str | None = None  # optional: pin and cross-check a specific expected paper account
    paper_account_prefix: str = DEFAULT_PAPER_ACCOUNT_PREFIX
    connect_timeout_seconds: float = 10.0

    def require_paper_port(self) -> None:
        if self.port not in PAPER_PORTS:
            reason = "a known LIVE port" if self.port in LIVE_PORTS else "not a recognized paper port"
            raise LiveTradingBlockedError(
                f"Refusing to connect: port {self.port} is {reason}. "
                f"Known paper ports are {sorted(PAPER_PORTS)}; live trading is not supported "
                f"by this adapter under any configuration."
            )


# --------------------------------------------------------------------
# IBClientLike: the narrow interface this module actually depends on.
# --------------------------------------------------------------------


class IBClientLike(Protocol):
    def connect(self, host: str, port: int, clientId: int, timeout: float) -> None: ...
    def disconnect(self) -> None: ...
    def isConnected(self) -> bool: ...
    def managedAccounts(self) -> list[str]: ...
    def accountSummary(self, account: str) -> list[Any]: ...  # items with .tag, .value
    def positions(self, account: str) -> list[Any]: ...  # .contract, .position, .avgCost
    def qualifyContracts(self, *contracts: Any) -> list[Any]: ...
    def reqTickers(self, *contracts: Any) -> list[Any]: ...  # .contract, .bid, .ask, .last, .volume, .modelGreeks
    def reqSecDefOptParams(self, symbol: str, underlyingConId: int) -> Any: ...  # .expirations, .strikes
    def placeOrder(self, contract: Any, order: Any) -> Any: ...  # Trade-like: .order, .orderStatus, .contract
    def cancelOrder(self, order: Any) -> None: ...
    def openTrades(self) -> list[Any]: ...
    def fills(self) -> list[Any]: ...  # .execution, .contract, .commissionReport, .time
    def next_order_id(self) -> int: ...


class _RealIBAdapter:
    """Wraps a real `ib_insync.IB()` instance to satisfy `IBClientLike`.
    Every place this codebase depends on ib_insync's actual API surface
    is isolated here — nowhere else in `ibkr.py` touches `ib_insync`
    directly."""

    def __init__(self, ib: Any) -> None:
        self._ib = ib

    def connect(self, host: str, port: int, clientId: int, timeout: float) -> None:
        self._ib.connect(host, port, clientId=clientId, timeout=timeout)

    def disconnect(self) -> None:
        self._ib.disconnect()

    def isConnected(self) -> bool:
        return bool(self._ib.isConnected())

    def managedAccounts(self) -> list[str]:
        return list(self._ib.managedAccounts())

    def accountSummary(self, account: str) -> list[Any]:
        return list(self._ib.accountSummary(account))

    def positions(self, account: str) -> list[Any]:
        return list(self._ib.positions(account))

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        ib_contracts = [self._to_ib_contract(c) for c in contracts]
        return list(self._ib.qualifyContracts(*ib_contracts))

    def reqTickers(self, *contracts: Any) -> list[Any]:
        return list(self._ib.reqTickers(*contracts))

    def reqSecDefOptParams(self, symbol: str, underlyingConId: int) -> Any:
        params = self._ib.reqSecDefOptParams(symbol, "", "STK", underlyingConId)
        return params[0] if params else None

    def placeOrder(self, contract: Any, order: Any) -> Any:
        return self._ib.placeOrder(self._to_ib_contract(contract), self._to_ib_order(order))

    def cancelOrder(self, order: Any) -> None:
        self._ib.cancelOrder(order)

    def _to_ib_contract(self, spec: Any) -> Any:
        """Translates this module's internal contract specs
        (`_StockSpec`/`_OptionSpec`/`_ComboSpec`) into real
        `ib_insync` contract objects. An object that's already a real
        ib_insync contract (e.g. one already round-tripped through a
        prior `qualifyContracts` call) passes through unchanged."""
        import ib_insync

        if isinstance(spec, _StockSpec):
            return ib_insync.Stock(spec.symbol, "SMART", "USD")
        if isinstance(spec, _OptionSpec):
            return ib_insync.Option(spec.symbol, spec.expiration.strftime("%Y%m%d"), spec.strike, spec.right, "SMART")
        if isinstance(spec, _ComboSpec):
            return self._to_combo_contract(spec)
        return spec

    def _to_combo_contract(self, spec: "_ComboSpec") -> Any:
        import ib_insync

        underlying_symbol = spec.legs[0].symbol
        combo_legs = []
        for leg in spec.legs:
            option = ib_insync.Option(leg.symbol, leg.expiration.strftime("%Y%m%d"), leg.strike, leg.right.value, "SMART")
            (qualified,) = self._ib.qualifyContracts(option)
            combo_legs.append(
                ib_insync.ComboLeg(
                    conId=qualified.conId,
                    ratio=leg.quantity,
                    action="BUY" if leg.action.value == "buy" else "SELL",
                    exchange="SMART",
                )
            )
        return ib_insync.Contract(symbol=underlying_symbol, secType="BAG", exchange="SMART", currency="USD", comboLegs=combo_legs)

    def _to_ib_order(self, spec: Any) -> Any:
        import ib_insync

        if isinstance(spec, _LimitOrderSpec):
            return ib_insync.LimitOrder(spec.action, spec.totalQuantity, spec.lmtPrice, orderRef=spec.orderRef, tif=spec.tif)
        return spec

    def openTrades(self) -> list[Any]:
        return list(self._ib.openTrades())

    def fills(self) -> list[Any]:
        return list(self._ib.fills())

    def next_order_id(self) -> int:
        return int(self._ib.client.getReqId())


def _build_default_client() -> IBClientLike:
    # Imported lazily so this module has no hard dependency on the
    # ib_insync package unless a real client is actually needed.
    import ib_insync

    return _RealIBAdapter(ib_insync.IB())


# --------------------------------------------------------------------
# Conversion helpers: raw ib_insync-shaped objects -> canonical schemas
# --------------------------------------------------------------------


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) or math.isinf(f) else f


def _safe_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _safe_int(value: Any, default: int = 0) -> int:
    f = _safe_float(value, default=float(default))
    return max(int(f), 0)


def _parse_ibkr_date(raw: str) -> date:
    return datetime.strptime(raw[:8], "%Y%m%d").date()


def ticker_to_underlying_quote(ticker: Any, symbol: str, timestamp: datetime) -> UnderlyingQuote:
    return UnderlyingQuote(
        symbol=symbol,
        bid=_safe_float(ticker.bid),
        ask=_safe_float(ticker.ask),
        last=_safe_float(ticker.last),
        volume=_safe_int(getattr(ticker, "volume", 0)),
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


def ticker_to_option_contract(ticker: Any, underlying_price: float, timestamp: datetime) -> OptionContract:
    contract = ticker.contract
    greeks = getattr(ticker, "modelGreeks", None)
    right = OptionRight.CALL if str(contract.right).upper().startswith("C") else OptionRight.PUT
    option_symbol = getattr(contract, "localSymbol", None) or (
        f"{contract.symbol}{contract.lastTradeDateOrContractMonth}{contract.right}{contract.strike}"
    )
    return OptionContract(
        underlying=contract.symbol,
        option_symbol=option_symbol,
        expiration=_parse_ibkr_date(contract.lastTradeDateOrContractMonth),
        strike=float(contract.strike),
        right=right,
        bid=_safe_float(ticker.bid),
        ask=_safe_float(ticker.ask),
        last=_safe_float(ticker.last),
        volume=_safe_int(getattr(ticker, "volume", 0)),
        open_interest=_safe_int(getattr(ticker, "openInterest", 0)),
        iv=_safe_optional_float(getattr(greeks, "impliedVol", None)) if greeks else None,
        delta=_safe_optional_float(getattr(greeks, "delta", None)) if greeks else None,
        gamma=_safe_optional_float(getattr(greeks, "gamma", None)) if greeks else None,
        theta=_safe_optional_float(getattr(greeks, "theta", None)) if greeks else None,
        vega=_safe_optional_float(getattr(greeks, "vega", None)) if greeks else None,
        underlying_price=underlying_price,
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


def ib_position_to_position(raw: Any, market_price: float, timestamp: datetime) -> Position:
    quantity = int(raw.position)
    avg_cost = _safe_float(raw.avgCost)
    market_value = quantity * market_price
    unrealized_pnl = market_value - quantity * avg_cost
    symbol = getattr(raw.contract, "localSymbol", None) or raw.contract.symbol
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_cost=avg_cost,
        market_price=market_price,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


_ACCOUNT_TAGS = {
    "NetLiquidation": "net_liquidation",
    "BuyingPower": "buying_power",
    "TotalCashValue": "cash_balance",
    "MaintMarginReq": "maintenance_margin",
}


def account_summary_to_account(account_id: str, raw_items: list[Any], timestamp: datetime) -> Account:
    values: dict[str, float] = {}
    currency = "USD"
    for item in raw_items:
        field_name = _ACCOUNT_TAGS.get(item.tag)
        if field_name:
            values[field_name] = _safe_float(item.value)
        if item.tag == "NetLiquidation" and getattr(item, "currency", None):
            currency = item.currency
    return Account(
        account_id=account_id,
        currency=currency,
        net_liquidation=values.get("net_liquidation", 0.0),
        buying_power=values.get("buying_power", 0.0),
        cash_balance=values.get("cash_balance", 0.0),
        maintenance_margin=values.get("maintenance_margin", 0.0),
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


def _order_status_from_ib(raw_status: str) -> OrderStatus:
    mapping = {
        "PendingSubmit": OrderStatus.PENDING_SUBMIT,
        "PreSubmitted": OrderStatus.SUBMITTED,
        "Submitted": OrderStatus.SUBMITTED,
        "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
        "Filled": OrderStatus.FILLED,
        "Cancelled": OrderStatus.CANCELLED,
        "ApiCancelled": OrderStatus.CANCELLED,
        "Inactive": OrderStatus.REJECTED,
    }
    return mapping.get(raw_status, OrderStatus.SUBMITTED)


def trade_to_order(trade: Any, request: PlaceOrderRequest, timestamp: datetime) -> Order:
    order_status = trade.orderStatus
    return Order(
        client_order_id=request.client_order_id,
        broker_order_id=str(trade.order.orderId),
        legs=request.legs,
        order_type=request.order_type,
        limit_price=request.limit_price,
        status=_order_status_from_ib(getattr(order_status, "status", "Submitted")),
        filled_quantity=_safe_int(getattr(order_status, "filled", 0)),
        avg_fill_price=_safe_optional_float(getattr(order_status, "avgFillPrice", None)),
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


def fill_to_canonical(raw: Any, timestamp: datetime) -> Fill:
    execution = raw.execution
    action = OrderAction.BUY if str(execution.side).upper().startswith("B") else OrderAction.SELL
    commission = 0.0
    report = getattr(raw, "commissionReport", None)
    if report is not None:
        commission = _safe_float(getattr(report, "commission", 0.0))
    symbol = getattr(raw.contract, "localSymbol", None) or raw.contract.symbol
    return Fill(
        broker_order_id=str(execution.orderId),
        client_order_id=getattr(raw.order, "orderRef", None) if hasattr(raw, "order") else None,
        symbol=symbol,
        action=action,
        quantity=abs(int(execution.shares)),
        price=_safe_float(execution.price),
        commission=commission,
        timestamp=timestamp,
        source=SOURCE_IBKR,
    )


# --------------------------------------------------------------------
# IBKRBroker
# --------------------------------------------------------------------


class IBKRBroker(Broker):
    def __init__(
        self,
        config: IBKRConfig | None = None,
        ib_client: IBClientLike | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._config = config or IBKRConfig()
        self._config.require_paper_port()
        self._ib = ib_client or _build_default_client()
        self._store = idempotency_store or InMemoryIdempotencyStore()
        self._verified_account_id: str | None = None

    # ---- connection -----------------------------------------------

    async def connect(self) -> None:
        self._config.require_paper_port()
        if self._ib.isConnected():
            return

        last_exc: Exception | None = None
        for attempt in range(_READ_RETRY_ATTEMPTS):
            try:
                self._ib.connect(
                    self._config.host, self._config.port, self._config.client_id, self._config.connect_timeout_seconds
                )
                self._verify_paper_account()
                return
            except LiveTradingBlockedError:
                raise  # never retry past a paper-account safety rejection
            except Exception as exc:  # noqa: BLE001 - genuinely broad: any transport failure is retryable here
                last_exc = exc
                await asyncio.sleep(_READ_RETRY_BASE_DELAY_SECONDS * (2**attempt))
        raise BrokerConnectionError(f"Failed to connect to IBKR after {_READ_RETRY_ATTEMPTS} attempts") from last_exc

    def _verify_paper_account(self) -> None:
        accounts = self._ib.managedAccounts()
        if not accounts:
            self._ib.disconnect()
            raise LiveTradingBlockedError("IBKR reported no managed accounts after connecting; refusing to proceed.")

        prefix = self._config.paper_account_prefix
        non_paper = [a for a in accounts if not a.startswith(prefix)]
        if non_paper:
            self._ib.disconnect()
            raise LiveTradingBlockedError(
                f"Refusing to proceed: account(s) {non_paper} do not use the paper-account prefix "
                f"{prefix!r}. This adapter only supports paper trading."
            )

        if self._config.account_id and self._config.account_id not in accounts:
            self._ib.disconnect()
            raise LiveTradingBlockedError(
                f"Configured account_id={self._config.account_id!r} was not among the accounts IBKR "
                f"reported ({accounts}); refusing to proceed rather than trade on an unexpected account."
            )

        self._verified_account_id = self._config.account_id or accounts[0]

    async def disconnect(self) -> None:
        self._ib.disconnect()

    def is_connected(self) -> bool:
        return self._ib.isConnected()

    async def health_check(self) -> ConnectionHealth:
        start = datetime.now(timezone.utc)
        connected = self._ib.isConnected()
        latency_ms = None
        if connected:
            latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
        return ConnectionHealth(
            connected=connected,
            environment=BrokerEnvironment.PAPER,
            account_id=self._verified_account_id,
            checked_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
        )

    def _require_connected(self) -> None:
        if not self._ib.isConnected():
            raise BrokerConnectionError("Not connected to IBKR")

    async def _with_read_retry(self, fn, *args, **kwargs):
        """Read-only calls may retry, including reconnecting between
        attempts — never used for place_order/cancel_order. Catches
        `BrokerConnectionError` (raised internally by
        `_require_connected`) alongside the builtin transient-transport
        exceptions a real socket-based connection can raise
        (`ConnectionError`, `TimeoutError`, `OSError`); anything else
        (a validation error, a genuine business-logic failure) is not a
        connectivity problem and is never retried."""
        last_exc: Exception | None = None
        for attempt in range(_READ_RETRY_ATTEMPTS):
            try:
                if not self._ib.isConnected():
                    await self.connect()
                return fn(*args, **kwargs)
            except (BrokerConnectionError, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                await asyncio.sleep(_READ_RETRY_BASE_DELAY_SECONDS * (2**attempt))
        raise BrokerConnectionError(f"Read failed after {_READ_RETRY_ATTEMPTS} attempts") from last_exc

    # ---- account / positions ---------------------------------------

    async def get_account(self) -> Account:
        self._require_connected()
        return await self._with_read_retry(self._get_account_sync)

    def _get_account_sync(self) -> Account:
        account_id = self._verified_account_id or self._ib.managedAccounts()[0]
        items = self._ib.accountSummary(account_id)
        return account_summary_to_account(account_id, items, datetime.now(timezone.utc))

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return await self._with_read_retry(self._get_positions_sync)

    def _get_positions_sync(self) -> list[Position]:
        account_id = self._verified_account_id or self._ib.managedAccounts()[0]
        raw_positions = self._ib.positions(account_id)
        timestamp = datetime.now(timezone.utc)
        result = []
        for raw in raw_positions:
            tickers = self._ib.reqTickers(raw.contract)
            market_price = _safe_float(tickers[0].last) if tickers else _safe_float(raw.avgCost)
            result.append(ib_position_to_position(raw, market_price, timestamp))
        return result

    # ---- market data -------------------------------------------------

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        self._require_connected()
        return await self._with_read_retry(self._get_underlying_quote_sync, symbol)

    def _get_underlying_quote_sync(self, symbol: str) -> UnderlyingQuote:
        contract_spec = _StockSpec(symbol)
        qualified = self._ib.qualifyContracts(contract_spec)
        tickers = self._ib.reqTickers(*qualified)
        if not tickers:
            raise BrokerConnectionError(f"No market data returned for {symbol!r}")
        return ticker_to_underlying_quote(tickers[0], symbol, datetime.now(timezone.utc))

    async def get_option_chain(self, symbol: str, *, moneyness_band: float = 0.30, max_expirations: int = 4) -> OptionChain:
        self._require_connected()
        return await self._with_read_retry(self._get_option_chain_sync, symbol, moneyness_band, max_expirations)

    def _get_option_chain_sync(self, symbol: str, moneyness_band: float, max_expirations: int) -> OptionChain:
        timestamp = datetime.now(timezone.utc)
        underlying = self._get_underlying_quote_sync(symbol)

        stock_spec = _StockSpec(symbol)
        (qualified_stock,) = self._ib.qualifyContracts(stock_spec)
        params = self._ib.reqSecDefOptParams(symbol, qualified_stock.conId)
        if params is None:
            return OptionChain(underlying=underlying, contracts=[], timestamp=timestamp, source=SOURCE_IBKR)

        expirations = [_parse_ibkr_date(e) for e in sorted(params.expirations)[:max_expirations]]
        low = underlying.mid * (1 - moneyness_band)
        high = underlying.mid * (1 + moneyness_band)
        strikes = sorted(s for s in params.strikes if low <= s <= high)

        option_specs = [
            _OptionSpec(symbol, expiration, strike, right)
            for expiration in expirations
            for strike in strikes
            for right in ("C", "P")
        ]
        if not option_specs:
            return OptionChain(underlying=underlying, contracts=[], timestamp=timestamp, source=SOURCE_IBKR)

        qualified_options = self._ib.qualifyContracts(*option_specs)
        tickers = self._ib.reqTickers(*qualified_options)
        contracts = [ticker_to_option_contract(t, underlying.mid, timestamp) for t in tickers]
        return OptionChain(underlying=underlying, contracts=contracts, timestamp=timestamp, source=SOURCE_IBKR)

    # ---- orders --------------------------------------------------------

    async def place_order(self, request: PlaceOrderRequest) -> Order:
        """See Broker.place_order — deliberately no retry wrapper here.
        Idempotency (not retry) is what makes a caller-initiated retry
        with the same client_order_id safe."""
        self._require_connected()

        existing = self._store.get(request.client_order_id)
        if existing is not None:
            return existing

        # Defense in depth against the "we submitted but crashed before
        # recording it locally" case: check the broker's own open orders
        # for one already tagged with this client_order_id (via orderRef)
        # before assuming none exists.
        broker_side = self._find_open_order_by_client_id(request.client_order_id)
        if broker_side is not None:
            self._store.save(broker_side)
            return broker_side

        ib_contract, ib_order = self._build_ib_order(request)
        trade = self._ib.placeOrder(ib_contract, ib_order)
        order = trade_to_order(trade, request, datetime.now(timezone.utc))
        self._store.save(order)
        return order

    @staticmethod
    def _trade_to_open_order(trade: Any, client_order_id: str | None = None) -> Order:
        """Shared conversion for any ib_insync-shaped Trade-like object
        into a canonical Order, used by both the open-orders listing and
        the by-client-id lookup so the mapping logic exists in one
        place. `lmtPrice` falls back to a nominal 0.01 only as a
        defensive guard against a missing/zero value on the raw trade —
        every real submitted limit order carries a genuine limit price;
        this never fires in practice."""
        resolved_client_order_id = client_order_id or getattr(trade.order, "orderRef", None) or str(trade.order.orderId)
        legs = [
            OrderLeg(
                symbol=trade.contract.symbol,
                action=OrderAction.BUY if trade.order.action == "BUY" else OrderAction.SELL,
                quantity=int(trade.order.totalQuantity),
            )
        ]
        return Order(
            client_order_id=resolved_client_order_id,
            broker_order_id=str(trade.order.orderId),
            legs=legs,
            order_type=OrderType.LIMIT,
            limit_price=_safe_float(getattr(trade.order, "lmtPrice", 0)) or 0.01,
            status=_order_status_from_ib(getattr(trade.orderStatus, "status", "Submitted")),
            filled_quantity=_safe_int(getattr(trade.orderStatus, "filled", 0)),
            timestamp=datetime.now(timezone.utc),
            source=SOURCE_IBKR,
        )

    def _find_open_order_by_client_id(self, client_order_id: str) -> Order | None:
        matches = [
            self._trade_to_open_order(trade, client_order_id)
            for trade in self._ib.openTrades()
            if getattr(trade.order, "orderRef", None) == client_order_id
        ]
        if len(matches) > 1:
            # A genuine invariant violation: this client_order_id should
            # map to at most one broker-side order. Loud failure, not a
            # silent pick-one — see DuplicateOrderError's docstring.
            raise DuplicateOrderError(
                f"client_order_id={client_order_id!r} matches {len(matches)} distinct broker-side orders "
                f"({[m.broker_order_id for m in matches]}); this should never happen."
            )
        return matches[0] if matches else None

    def _build_ib_order(self, request: PlaceOrderRequest) -> tuple[Any, Any]:
        if len(request.legs) == 1:
            leg = request.legs[0]
            contract_spec = (
                _OptionSpec(leg.symbol, leg.expiration, leg.strike, leg.right.value)
                if leg.right is not None
                else _StockSpec(leg.symbol)
            )
            qualified_list = self._ib.qualifyContracts(contract_spec)
            contract = qualified_list[0] if qualified_list else contract_spec
        else:
            # Combo (multi-leg) orders aren't qualified the same way as
            # a single contract — the combo spec carries each leg's own
            # already-known symbol/strike/expiration/right directly.
            contract = _ComboSpec(request.legs)

        order = _LimitOrderSpec(
            action="BUY" if request.legs[0].action == OrderAction.BUY else "SELL",
            totalQuantity=request.legs[0].quantity,
            lmtPrice=request.limit_price,
            orderRef=request.client_order_id,
            tif=request.time_in_force,
        )
        return contract, order

    async def cancel_order(self, client_order_id: str) -> Order:
        """Deliberately no retry wrapper — see Broker.cancel_order."""
        self._require_connected()
        order = self._store.get(client_order_id)
        if order is None:
            raise ValueError(f"No known order for client_order_id={client_order_id!r}")
        if order.is_terminal:
            return order  # already in a terminal state; cancelling again is a no-op, not an error

        for trade in self._ib.openTrades():
            if getattr(trade.order, "orderRef", None) == client_order_id:
                self._ib.cancelOrder(trade.order)
                updated = Order(
                    client_order_id=client_order_id,
                    broker_order_id=order.broker_order_id,
                    legs=order.legs,
                    order_type=order.order_type,
                    limit_price=order.limit_price,
                    status=OrderStatus.CANCELLED,
                    filled_quantity=order.filled_quantity,
                    avg_fill_price=order.avg_fill_price,
                    timestamp=datetime.now(timezone.utc),
                    source=SOURCE_IBKR,
                )
                self._store.save(updated)
                return updated

        raise ValueError(f"client_order_id={client_order_id!r} is not among the broker's open orders")

    async def get_open_orders(self) -> list[Order]:
        self._require_connected()
        return await self._with_read_retry(self._get_open_orders_sync)

    def _get_open_orders_sync(self) -> list[Order]:
        orders = [self._trade_to_open_order(trade) for trade in self._ib.openTrades()]
        return orders

    async def get_fills(self, since: datetime | None = None) -> list[Fill]:
        self._require_connected()
        return await self._with_read_retry(self._get_fills_sync, since)

    def _get_fills_sync(self, since: datetime | None) -> list[Fill]:
        timestamp = datetime.now(timezone.utc)
        fills = [fill_to_canonical(raw, timestamp) for raw in self._ib.fills()]
        if since is not None:
            fills = [f for f in fills if f.timestamp >= since]
        return fills

    # ---- reconciliation --------------------------------------------

    async def reconcile(self) -> ReconciliationReport:
        self._require_connected()
        return await self._with_read_retry(self._reconcile_sync)

    def _reconcile_sync(self) -> ReconciliationReport:
        broker_orders = self._get_open_orders_sync()
        # Compare non-terminal to non-terminal: a broker order that has
        # already reached a terminal state but hasn't yet been pruned
        # from the broker's own open-trades listing isn't a meaningful
        # discrepancy against a local record we've already marked
        # terminal for the same reason.
        broker_by_client_id = {o.client_order_id: o for o in broker_orders if not o.is_terminal}
        local_orders = [o for o in self._store.all() if not o.is_terminal]

        discrepancies: list[ReconciliationDiscrepancy] = []
        for local in local_orders:
            if local.client_order_id not in broker_by_client_id:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="orphaned_local",
                        client_order_id=local.client_order_id,
                        broker_order_id=local.broker_order_id,
                        detail="Order tracked locally as open but not found among the broker's open orders.",
                    )
                )
            elif broker_by_client_id[local.client_order_id].status != local.status:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="status_mismatch",
                        client_order_id=local.client_order_id,
                        broker_order_id=local.broker_order_id,
                        detail=(
                            f"Local status={local.status.value!r} vs broker status="
                            f"{broker_by_client_id[local.client_order_id].status.value!r}."
                        ),
                    )
                )

        local_ids = {o.client_order_id for o in local_orders}
        for client_order_id, broker_order in broker_by_client_id.items():
            if client_order_id not in local_ids:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="unknown_broker",
                        client_order_id=client_order_id,
                        broker_order_id=broker_order.broker_order_id,
                        detail="Order open at the broker with no matching local record.",
                    )
                )

        return ReconciliationReport(checked_at=datetime.now(timezone.utc), discrepancies=discrepancies)


# --------------------------------------------------------------------
# Minimal contract/order spec value objects passed to IBClientLike.
# These are intentionally NOT ib_insync types — _RealIBAdapter's
# caller (this module) builds them; a production _RealIBAdapter would
# translate these into real ib_insync Contract/Order objects, but since
# qualifyContracts/placeOrder are themselves behind the Protocol, the
# translation only needs to happen once, inside _RealIBAdapter, not
# duplicated at every call site.
# --------------------------------------------------------------------


class _StockSpec:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.secType = "STK"


class _OptionSpec:
    def __init__(self, symbol: str, expiration: date, strike: float, right: str) -> None:
        self.symbol = symbol
        self.expiration = expiration  # a `date`; _RealIBAdapter formats it for the real API
        self.strike = strike
        self.right = right
        self.secType = "OPT"


class _ComboSpec:
    def __init__(self, legs: list[OrderLeg]) -> None:
        self.legs = legs
        self.secType = "BAG"


class _LimitOrderSpec:
    """Deliberately uses ib_insync's own `Order` attribute names
    (`totalQuantity`, `lmtPrice`, `orderRef`, `tif`), not a fresh
    snake_case invention — both a fake test double that simply echoes
    this object back as `trade.order` (a decent approximation of real
    ib_insync behavior, which likewise attaches `orderId` to the same
    object you submitted) and a real `ib_insync.Order` expose the same
    attribute names, so the conversion helpers below (`_trade_to_open_
    order`, `trade_to_order`) work against either without a branch."""

    def __init__(self, action: str, totalQuantity: int, lmtPrice: float, orderRef: str, tif: str) -> None:
        self.action = action
        self.totalQuantity = totalQuantity
        self.lmtPrice = lmtPrice
        self.orderRef = orderRef
        self.tif = tif
        self.orderId: int | None = None
