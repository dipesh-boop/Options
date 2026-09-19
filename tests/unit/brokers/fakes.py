"""A fake IBClientLike implementation shared across the broker test
suite. No test in this package needs a real IBKR connection, TWS/Gateway
instance, or the `ib_insync` package at all — this fake stands in for
all of it."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

DEFAULT_ACCOUNT_SUMMARY = [
    SimpleNamespace(tag="NetLiquidation", value="100000", currency="USD"),
    SimpleNamespace(tag="BuyingPower", value="200000", currency="USD"),
    SimpleNamespace(tag="TotalCashValue", value="50000", currency="USD"),
    SimpleNamespace(tag="MaintMarginReq", value="1500", currency="USD"),
]


def make_underlying_ticker(symbol: str, bid: float = 224.9, ask: float = 225.1, last: float = 225.0, volume: int = 1_000_000) -> Any:
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), bid=bid, ask=ask, last=last, volume=volume, modelGreeks=None)


def make_option_ticker(
    symbol: str,
    expiration: date,
    strike: float,
    right: str,
    *,
    bid: float = 2.40,
    ask: float = 2.60,
    last: float = 2.50,
    volume: int = 100,
    open_interest: int = 500,
    iv: float = 0.28,
    delta: float = -0.30,
    gamma: float = 0.02,
    theta: float = -0.05,
    vega: float = 0.15,
) -> Any:
    return SimpleNamespace(
        contract=SimpleNamespace(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiration.strftime("%Y%m%d"),
            strike=strike,
            right=right,
            localSymbol=None,
        ),
        bid=bid,
        ask=ask,
        last=last,
        volume=volume,
        openInterest=open_interest,
        modelGreeks=SimpleNamespace(impliedVol=iv, delta=delta, gamma=gamma, theta=theta, vega=vega),
    )


class FakeIBClient:
    """A realistic-enough stand-in for IBClientLike. Supports injecting
    transient failures (to exercise retry) and tracks submitted orders
    with real-ish lifecycle semantics (cancel actually changes status;
    openTrades() excludes terminal orders, matching real ib_insync
    behavior)."""

    def __init__(
        self,
        *,
        managed_accounts: tuple[str, ...] = ("DU1234567",),
        account_summary_items: list[Any] | None = None,
        fail_connect_times: int = 0,
        fail_read_times: int = 0,
        positions: list[Any] | None = None,
        option_params: dict[str, Any] | None = None,
    ) -> None:
        self._connected = False
        self.managed_accounts_list = list(managed_accounts)
        self._account_summary_items = account_summary_items if account_summary_items is not None else DEFAULT_ACCOUNT_SUMMARY
        self._fail_connect_times = fail_connect_times
        self._connect_attempts = 0
        self._fail_read_times = fail_read_times
        self._read_attempts = 0
        self.orders: list[Any] = []
        self._next_order_id = 1000
        self._fills: list[Any] = []
        self._positions = positions or []
        self._option_params = option_params or {}
        self.connect_call_count = 0
        self.place_order_call_count = 0
        self.cancel_order_call_count = 0

    # ---- connection -------------------------------------------------

    def connect(self, host: str, port: int, clientId: int, timeout: float) -> None:
        self.connect_call_count += 1
        self._connect_attempts += 1
        if self._connect_attempts <= self._fail_connect_times:
            raise ConnectionError("simulated transient connect failure")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def isConnected(self) -> bool:
        return self._connected

    def managedAccounts(self) -> list[str]:
        return list(self.managed_accounts_list)

    # ---- account / positions ----------------------------------------

    def accountSummary(self, account: str) -> list[Any]:
        self._maybe_fail_read()
        return list(self._account_summary_items)

    def positions(self, account: str) -> list[Any]:
        self._maybe_fail_read()
        return list(self._positions)

    def _maybe_fail_read(self) -> None:
        self._read_attempts += 1
        if self._read_attempts <= self._fail_read_times:
            self._connected = False  # simulate a dropped connection alongside the failure
            raise ConnectionError("simulated transient read failure")

    # ---- market data --------------------------------------------------

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        for c in contracts:
            if not hasattr(c, "conId"):
                c.conId = abs(hash((getattr(c, "symbol", ""), getattr(c, "strike", None)))) % 1_000_000
        return list(contracts)

    def reqTickers(self, *contracts: Any) -> list[Any]:
        self._maybe_fail_read()
        out = []
        for c in contracts:
            if getattr(c, "secType", None) == "OPT":
                out.append(make_option_ticker(c.symbol, c.expiration, c.strike, c.right))
            else:
                out.append(make_underlying_ticker(c.symbol))
        return out

    def reqSecDefOptParams(self, symbol: str, underlyingConId: int) -> Any:
        return self._option_params.get(symbol)

    # ---- orders -----------------------------------------------------

    def placeOrder(self, contract: Any, order: Any) -> Any:
        self.place_order_call_count += 1
        self._next_order_id += 1
        order.orderId = self._next_order_id
        trade = SimpleNamespace(
            order=order,
            orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=None),
            contract=contract,
        )
        self.orders.append(trade)
        return trade

    def cancelOrder(self, order: Any) -> None:
        self.cancel_order_call_count += 1
        for trade in self.orders:
            if trade.order.orderId == order.orderId:
                trade.orderStatus.status = "Cancelled"

    def openTrades(self) -> list[Any]:
        return [t for t in self.orders if t.orderStatus.status not in ("Filled", "Cancelled")]

    def fills(self) -> list[Any]:
        return list(self._fills)

    def add_fill(self, *, order_id: int, symbol: str, side: str, shares: int, price: float, commission: float = 0.65) -> None:
        self._fills.append(
            SimpleNamespace(
                execution=SimpleNamespace(orderId=order_id, side=side, shares=shares, price=price),
                contract=SimpleNamespace(symbol=symbol, localSymbol=None),
                commissionReport=SimpleNamespace(commission=commission),
            )
        )


def make_option_params(expirations: list[str], strikes: list[float]) -> Any:
    return SimpleNamespace(expirations=expirations, strikes=strikes)
