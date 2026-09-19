"""Internal paper-execution simulator (Step 12).

We are not executing options automatically through Fidelity — Fidelity
stays `MANUAL_EXECUTION` (`src.brokers.fidelity`), untouched by this
module. `PaperBroker` is a second, independent execution path: a
`src.brokers.base.Broker` implementation that simulates fills against
this platform's own market data, for research/backtesting/paper-trading
purposes, exactly like `src.brokers.ibkr.IBKRBroker` simulates against a
real paper account. Nothing here ever reaches a real market or a real
brokerage account.

**Realistic fills, not midpoint fantasy.** A limit order does not
automatically get the midpoint just because that's the "fair" price —
see `FillModel` and `compute_fill`. The default (`LIQUIDITY_ADJUSTED`)
is deliberately conservative: thin volume/open interest and wide
spreads make a fill worse (or a partial fill, or no fill), never better.

**Simplification, stated plainly**: options are cash-settled at
expiration by intrinsic value; assignment/exercise still move real
simulated share positions and cash (a short put assigned buys shares at
strike, a short call assigned sells them, mirrored for a long leg
exercised) — see `settle_expiration`. What this module does not do is
model early assignment before expiration (a real, if uncommon, American-
option risk `src.risk.stress`/the Devil's Advocate persona already flag
qualitatively) or partial-lot share assignment below 100 shares.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from src.brokers.base import (
    Account,
    Broker,
    ConnectionHealth,
    BrokerEnvironment,
    DuplicateOrderError,
    Fill,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    PlaceOrderRequest,
    Position,
    ReconciliationReport,
)
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import StaleDataError
from src.data.quotes import UnderlyingQuote

SOURCE_PAPER = "paper"
_CONTRACT_MULTIPLIER = 100


class FillModel(str, Enum):
    """How a resting limit order's price is simulated against the
    current market. BID/ASK are deliberately named for what each leg
    fills at, not for whether that's good or bad for the trader — for a
    net-credit combo (this platform's only kind of opening order),
    filling every leg at BID nets a *worse* (lower) credit than MID, and
    every leg at ASK nets a *better* (higher) credit; for a debit
    (closing) order the relationship reverses. That asymmetry is the
    point: BID is the conservative case for the common opening trade,
    which is exactly why it is not the default."""

    BID = "bid"
    ASK = "ask"
    MID = "mid"
    MID_WITH_SLIPPAGE = "mid_with_slippage"
    LIQUIDITY_ADJUSTED = "liquidity_adjusted"


@dataclass(frozen=True)
class PaperBrokerConfig:
    """Simulation assumptions, not a risk limit — nothing here gates a
    real trade the way `src.risk.limits.RiskLimitsConfig` does, so
    (consistent with `src.quant.monte_carlo`'s `STANDARD_SPOT_SHOCKS`)
    these are plain, overridable defaults rather than a YAML file."""

    fill_model: FillModel = FillModel.LIQUIDITY_ADJUSTED
    commission_per_contract: float = 0.65
    slippage_bps: float = 200.0  # 2% of the net mid, applied against the trader — a floor, not the only driver
    spread_capture_fraction: float = 0.25  # fraction of each leg's own bid/ask spread given up as slippage
    thin_volume_threshold: int = 50
    thin_open_interest_threshold: int = 200
    thin_liquidity_penalty_multiplier: float = 3.0
    max_fill_fraction_of_volume: float = 0.10  # a paper fill can't consume more than 10% of the leg's daily volume
    min_guaranteed_fill_contracts: int = 10  # floor on fillable size when open interest is healthy
    max_quote_age: timedelta = timedelta(minutes=15)


class InsufficientCashError(RuntimeError):
    """Raised internally, caught by `place_order`, which converts it
    into a REJECTED `Order` — callers get a normal `Order` object back,
    never an exception, for a rejection reason ordinary business logic
    (not a system fault) produces."""


class NoQuoteAvailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegQuote:
    symbol: str
    bid: float
    ask: float
    volume: int
    open_interest: int
    multiplier: int  # 100 for an option leg, 1 for an equity leg


def _leg_mid(q: LegQuote) -> float:
    if q.bid <= 0 and q.ask <= 0:
        return 0.0
    return (q.bid + q.ask) / 2.0


def _leg_spread_pct(q: LegQuote) -> float:
    mid = _leg_mid(q)
    if mid <= 0:
        return math.inf
    return (q.ask - q.bid) / mid


def _leg_price_for_model(q: LegQuote, model: FillModel) -> float:
    if model == FillModel.BID:
        return q.bid
    if model == FillModel.ASK:
        return q.ask
    return _leg_mid(q)


@dataclass(frozen=True)
class FillQuote:
    """The net, whole-order picture a fill decision is made from."""

    net_base_price: float  # net price before any slippage/liquidity adjustment, signed (+ = credit to trader)
    net_price: float  # the actual simulated fill price after model adjustment
    leg_prices: list[float]  # per-leg fill prices, same order as the request's legs, summing (signed) to net_price
    min_leg_volume: int
    min_leg_open_interest: int
    max_leg_spread_pct: float
    is_credit: bool


def compute_fill(legs: list[OrderLeg], leg_quotes: list[LegQuote], config: PaperBrokerConfig) -> FillQuote:
    """Pure function: given an order's legs and their current quotes,
    computes the simulated net fill price under `config.fill_model` —
    the deterministic core `PaperBroker.place_order`/`attempt_fill` call.
    """
    if len(legs) != len(leg_quotes):
        raise ValueError("legs and leg_quotes must be the same length")

    signs = [1 if leg.action == OrderAction.SELL else -1 for leg in legs]
    base_prices = [_leg_price_for_model(q, config.fill_model) for q in leg_quotes]
    net_base = sum(s * p for s, p in zip(signs, base_prices))

    min_vol = min((q.volume for q in leg_quotes), default=0)
    min_oi = min((q.open_interest for q in leg_quotes), default=0)
    max_spread_pct = max((_leg_spread_pct(q) for q in leg_quotes), default=0.0)

    if config.fill_model in (FillModel.BID, FillModel.ASK, FillModel.MID):
        return FillQuote(
            net_base_price=net_base,
            net_price=net_base,
            leg_prices=base_prices,
            min_leg_volume=min_vol,
            min_leg_open_interest=min_oi,
            max_leg_spread_pct=max_spread_pct,
            is_credit=net_base > 0,
        )

    # MID_WITH_SLIPPAGE / LIQUIDITY_ADJUSTED: start from mid, then move
    # the net price against the trader. Slippage is the larger of a
    # floor (bps of the net mid) and a fraction of the actual bid/ask
    # spread — a wide market costs more to trade regardless of how
    # thick the mid itself is, which a bps-of-mid-only model would miss
    # entirely (a wide spread with the same mid as a tight one would
    # otherwise simulate identically).
    mids = [_leg_mid(q) for q in leg_quotes]
    net_mid = sum(s * m for s, m in zip(signs, mids))
    avg_leg_spread = sum(q.ask - q.bid for q in leg_quotes) / len(leg_quotes)
    slippage_from_bps = max(abs(net_mid), 0.01) * (config.slippage_bps / 10_000.0)
    slippage_from_spread = avg_leg_spread * config.spread_capture_fraction
    slippage_amount = max(slippage_from_bps, slippage_from_spread)

    if config.fill_model == FillModel.LIQUIDITY_ADJUSTED and (
        min_vol < config.thin_volume_threshold or min_oi < config.thin_open_interest_threshold
    ):
        slippage_amount *= config.thin_liquidity_penalty_multiplier

    net_price = net_mid - slippage_amount
    adjustment_per_leg = -slippage_amount / len(legs)
    leg_prices = [m + s * adjustment_per_leg for m, s in zip(mids, signs)]

    return FillQuote(
        net_base_price=net_base,
        net_price=net_price,
        leg_prices=leg_prices,
        min_leg_volume=min_vol,
        min_leg_open_interest=min_oi,
        max_leg_spread_pct=max_spread_pct,
        is_credit=net_mid > 0,
    )


def fillable_quantity(requested: int, fill_quote: FillQuote, config: PaperBrokerConfig) -> int:
    """How much of a `requested` order size the simulated market can
    absorb right now — the mechanism behind partial fills."""
    by_volume = math.floor(fill_quote.min_leg_volume * config.max_fill_fraction_of_volume)
    if fill_quote.min_leg_open_interest >= config.thin_open_interest_threshold:
        by_volume = max(by_volume, config.min_guaranteed_fill_contracts)
    return max(min(requested, by_volume), 0)


def price_satisfies_limit(fill_quote: FillQuote, limit_price: float) -> bool:
    """A net-credit order fills only at or above its limit (a floor on
    credit received); a net-debit order fills only at or below its limit
    (a ceiling on debit paid) — ordinary limit-order semantics, applied
    to the whole combo's net price. `net_price` is negative for a debit
    (cash paid out), so "pay no more than limit_price" is
    `net_price >= -limit_price` (e.g. paying $0.50 against a $0.60 max
    is -0.50 >= -0.60, true) — not the other way around."""
    if fill_quote.is_credit:
        return fill_quote.net_price >= limit_price
    return fill_quote.net_price >= -limit_price


@dataclass(frozen=True)
class ExpirationSettlement:
    symbol: str
    right: OptionRight
    strike: float
    expiration: date
    was_itm: bool
    assigned_or_exercised: bool
    cash_impact: float
    share_impact: int  # signed change to the underlying equity position, 0 if cash-settled without shares


class PaperBroker(Broker):
    """A `Broker` implementation backed entirely by in-memory simulation.
    Market data is supplied explicitly via `update_market_data` (this
    module has no network client of any kind) — a caller (or a test)
    feeds it quotes the same way a live feed handler would."""

    def __init__(
        self,
        *,
        initial_cash: float,
        account_id: str = "PAPER-1",
        config: PaperBrokerConfig | None = None,
        idempotency_store: IdempotencyStore | None = None,
        now: datetime | None = None,
    ) -> None:
        if initial_cash < 0:
            raise ValueError("initial_cash cannot be negative")
        self._cash = initial_cash
        self._reserved_collateral = 0.0
        self._account_id = account_id
        self._config = config or PaperBrokerConfig()
        self._idempotency = idempotency_store or InMemoryIdempotencyStore()
        self._now = now or datetime.now().astimezone()
        self._connected = False
        self._positions: dict[str, Position] = {}  # symbol -> Position (options and equity alike)
        self._chains: dict[str, OptionChain] = {}  # underlying symbol -> latest OptionChain
        self._underlyings: dict[str, UnderlyingQuote] = {}
        self._fills: list[Fill] = []
        self._rejection_reasons: dict[str, str] = {}
        self._order_legs_cache: dict[str, list[OrderLeg]] = {}

    # ---------------------------------------------------------- market data

    def set_clock(self, now: datetime) -> None:
        self._now = now

    def update_market_data(self, chain: OptionChain) -> None:
        """Feeds this simulator a fresh option chain (and its embedded
        underlying quote). The only way market data enters this module —
        there is no provider client here to fetch it automatically."""
        self._chains[chain.underlying.symbol] = chain
        self._underlyings[chain.underlying.symbol] = chain.underlying

    def get_rejection_reason(self, client_order_id: str) -> str | None:
        return self._rejection_reasons.get(client_order_id)

    # -------------------------------------------------------------- Broker

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def health_check(self) -> ConnectionHealth:
        return ConnectionHealth(
            connected=self._connected,
            environment=BrokerEnvironment.PAPER,
            account_id=self._account_id,
            checked_at=self._now,
            latency_ms=0.0,
        )

    async def get_account(self) -> Account:
        market_value = sum(p.market_value for p in self._positions.values())
        return Account(
            account_id=self._account_id,
            currency="USD",
            net_liquidation=self._cash + market_value,
            buying_power=max(self._cash - self._reserved_collateral, 0.0),
            cash_balance=self._cash,
            maintenance_margin=self._reserved_collateral,
            timestamp=self._now,
            source=SOURCE_PAPER,
        )

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        quote = self._underlyings.get(symbol)
        if quote is None:
            raise NoQuoteAvailableError(f"no underlying quote loaded for {symbol!r}")
        return quote

    async def get_option_chain(self, symbol: str) -> OptionChain:
        chain = self._chains.get(symbol)
        if chain is None:
            raise NoQuoteAvailableError(f"no option chain loaded for {symbol!r}")
        return chain

    async def place_order(self, request: PlaceOrderRequest) -> Order:
        existing = self._idempotency.get(request.client_order_id)
        if existing is not None:
            return existing

        order = Order(
            client_order_id=request.client_order_id,
            broker_order_id=str(uuid.uuid4()),
            legs=request.legs,
            order_type=request.order_type,
            limit_price=request.limit_price,
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
            avg_fill_price=None,
            timestamp=self._now,
            source=SOURCE_PAPER,
        )
        self._order_legs_cache[request.client_order_id] = request.legs
        self._idempotency.save(order)

        try:
            self._validate_collateral(request)
        except InsufficientCashError as exc:
            rejected = order.model_copy(update={"status": OrderStatus.REJECTED})
            self._idempotency.save(rejected)
            self._rejection_reasons[request.client_order_id] = str(exc)
            return rejected

        return await self.attempt_fill(request.client_order_id)

    async def attempt_fill(self, client_order_id: str) -> Order:
        """Re-checks a resting (SUBMITTED or PARTIALLY_FILLED) order
        against the currently loaded market data. Called once
        automatically at the end of `place_order`; callers may call it
        again after `update_market_data` to simulate the market catching
        up to a limit order that didn't fill immediately."""
        order = self._idempotency.get(client_order_id)
        if order is None:
            raise KeyError(f"no order on file for client_order_id={client_order_id!r}")
        if order.is_terminal:
            return order

        try:
            leg_quotes = [self._resolve_leg_quote(leg) for leg in order.legs]
        except (NoQuoteAvailableError, StaleDataError) as exc:
            rejected = order.model_copy(update={"status": OrderStatus.REJECTED})
            self._idempotency.save(rejected)
            self._rejection_reasons[client_order_id] = str(exc)
            return rejected

        fill_quote = compute_fill(order.legs, leg_quotes, self._config)
        remaining = order.legs[0].quantity - order.filled_quantity
        if not price_satisfies_limit(fill_quote, order.limit_price):
            return order  # still resting, no fill this attempt

        qty = fillable_quantity(remaining, fill_quote, self._config)
        if qty <= 0:
            return order  # priced fine, but no liquidity right now

        self._apply_fill(order, fill_quote, qty)
        new_filled = order.filled_quantity + qty
        new_status = OrderStatus.FILLED if new_filled >= order.legs[0].quantity else OrderStatus.PARTIALLY_FILLED
        prior_notional = (order.avg_fill_price or 0.0) * order.filled_quantity
        new_avg = (prior_notional + fill_quote.net_price * qty) / new_filled
        updated = order.model_copy(update={"status": new_status, "filled_quantity": new_filled, "avg_fill_price": new_avg})
        self._idempotency.save(updated)
        return updated

    async def cancel_order(self, client_order_id: str) -> Order:
        order = self._idempotency.get(client_order_id)
        if order is None:
            raise KeyError(f"no order on file for client_order_id={client_order_id!r}")
        if order.is_terminal:
            return order
        cancelled = order.model_copy(update={"status": OrderStatus.CANCELLED})
        self._idempotency.save(cancelled)
        return cancelled

    async def get_open_orders(self) -> list[Order]:
        return [o for o in self._idempotency.all() if not o.is_terminal]

    async def get_fills(self, since: datetime | None = None) -> list[Fill]:
        if since is None:
            return list(self._fills)
        return [f for f in self._fills if f.timestamp >= since]

    async def reconcile(self) -> ReconciliationReport:
        # A paper broker's local state IS the broker's state by
        # construction — there is nothing external to drift from, so
        # reconciliation is always clean.
        return ReconciliationReport(checked_at=self._now, discrepancies=[])

    # ------------------------------------------------------------ internals

    def _resolve_leg_quote(self, leg: OrderLeg) -> LegQuote:
        if leg.right is None:
            # Equity leg: look up by symbol across loaded underlyings.
            quote = self._underlyings.get(leg.symbol)
            if quote is None:
                raise NoQuoteAvailableError(f"no underlying quote loaded for {leg.symbol!r}")
            quote.require_fresh(self._now, max_age=self._config.max_quote_age)
            return LegQuote(symbol=leg.symbol, bid=quote.bid, ask=quote.ask, volume=quote.volume, open_interest=0, multiplier=1)

        contract = self._find_contract(leg.symbol)
        contract.require_fresh(self._now, max_age=self._config.max_quote_age)
        return LegQuote(
            symbol=leg.symbol,
            bid=contract.bid,
            ask=contract.ask,
            volume=contract.volume,
            open_interest=contract.open_interest,
            multiplier=_CONTRACT_MULTIPLIER,
        )

    def _find_contract(self, option_symbol: str) -> OptionContract:
        for chain in self._chains.values():
            for contract in chain.contracts:
                if contract.option_symbol == option_symbol:
                    return contract
        raise NoQuoteAvailableError(f"no contract loaded for option_symbol={option_symbol!r}")

    def _validate_collateral(self, request: PlaceOrderRequest) -> None:
        required = self._required_collateral(request.legs)
        available = self._cash - self._reserved_collateral
        if required > available:
            raise InsufficientCashError(
                f"required collateral ${required:,.2f} exceeds available buying power ${available:,.2f}"
            )

    def _required_collateral(self, legs: list[OrderLeg]) -> float:
        """A simplified, per-order collateral estimate for this
        platform's three strategies:

        - A short leg paired with a same-right, opposite-strike long leg
          in the *same* order (a put credit spread) is defined-risk
          margin: the strike width x 100 x qty, never the full
          cash-secured amount — netting against the long leg is the
          entire point of trading a spread instead of a naked put.
        - An unpaired short put requires strike x 100 x qty cash-secured
          (a cash-secured put).
        - An unpaired short call requires 100 x qty shares already held
          (a covered call — checked via `_has_covering_shares`, not a
          cash requirement) or, if uncovered, the same cash-secured
          amount as a conservative stand-in (this platform never
          proposes a naked call; this is a defensive fallback, not an
          endorsement of one).
        - A long leg needs only its own debit, already realized through
          the fill's cash settlement — never collateral."""
        short_legs = [leg for leg in legs if leg.action == OrderAction.SELL and leg.right is not None and leg.strike is not None]
        long_legs = [leg for leg in legs if leg.action == OrderAction.BUY and leg.right is not None and leg.strike is not None]

        total = 0.0
        paired_long_ids = set()
        for short in short_legs:
            paired_long = next(
                (
                    i
                    for i, long in enumerate(long_legs)
                    if i not in paired_long_ids and long.right == short.right and long.quantity == short.quantity
                ),
                None,
            )
            if paired_long is not None:
                long = long_legs[paired_long]
                paired_long_ids.add(paired_long)
                total += abs(short.strike - long.strike) * _CONTRACT_MULTIPLIER * short.quantity
                continue
            if short.right == OptionRight.CALL and self._has_covering_shares(short):
                continue
            total += short.strike * _CONTRACT_MULTIPLIER * short.quantity
        return total

    def _has_covering_shares(self, leg: OrderLeg) -> bool:
        underlying = self._underlying_symbol_for(leg.symbol)
        shares = self._positions.get(underlying)
        return shares is not None and shares.quantity >= _CONTRACT_MULTIPLIER * leg.quantity

    @staticmethod
    def _underlying_symbol_for(option_symbol: str) -> str:
        # OCC-style symbols are prefixed with the underlying ticker
        # (e.g. "SPY261016P00620000" -> "SPY"): strip the trailing
        # date+right+strike block, which always starts at the first
        # digit.
        for i, ch in enumerate(option_symbol):
            if ch.isdigit():
                return option_symbol[:i]
        return option_symbol

    def _apply_fill(self, order: Order, fill_quote: FillQuote, qty: int) -> None:
        for leg, leg_price in zip(order.legs, fill_quote.leg_prices):
            multiplier = _CONTRACT_MULTIPLIER if leg.right is not None else 1
            signed_qty = qty if leg.action == OrderAction.BUY else -qty
            cash_flow = -signed_qty * leg_price * multiplier
            commission = self._config.commission_per_contract * qty
            self._cash += cash_flow - commission
            self._update_position(leg.symbol, signed_qty, leg_price, multiplier)

            fill = Fill(
                broker_order_id=order.broker_order_id or "",
                client_order_id=order.client_order_id,
                symbol=leg.symbol,
                action=leg.action,
                quantity=qty,
                price=leg_price if leg_price > 0 else 0.0001,
                commission=commission,
                timestamp=self._now,
                source=SOURCE_PAPER,
            )
            self._fills.append(fill)

        self._recompute_collateral()

    def _update_position(self, symbol: str, signed_qty: int, price: float, multiplier: int) -> None:
        existing = self._positions.get(symbol)
        if existing is None:
            new_qty = signed_qty
            avg_cost = abs(price)
        else:
            new_qty = existing.quantity + signed_qty
            if new_qty == 0:
                self._positions.pop(symbol, None)
                return
            if (existing.quantity >= 0) == (signed_qty >= 0):
                total_cost = existing.avg_cost * abs(existing.quantity) + abs(price) * abs(signed_qty)
                avg_cost = total_cost / abs(new_qty)
            else:
                avg_cost = existing.avg_cost  # reducing/flipping: keep the original basis for the remainder

        market_price = abs(price)
        self._positions[symbol] = Position(
            symbol=symbol,
            quantity=new_qty,
            avg_cost=avg_cost,
            market_price=market_price,
            market_value=new_qty * market_price * multiplier,
            unrealized_pnl=(market_price - avg_cost) * new_qty * multiplier,
            timestamp=self._now,
            source=SOURCE_PAPER,
        )

    def _recompute_collateral(self) -> None:
        """Recomputed from the whole position book, not tracked
        incrementally per order — a put credit spread's two legs might
        have been opened in separate orders (or the same one), and
        `_required_collateral`'s paired-spread netting only works when
        it sees both legs together. Grouping open option positions by
        (underlying, expiration) and re-deriving synthetic legs from
        each position's current sign/quantity reconstructs that pairing
        regardless of how the positions were actually built up."""
        groups: dict[tuple[str, date], list[OrderLeg]] = {}
        for symbol, position in self._positions.items():
            if symbol in self._underlyings or position.quantity == 0:
                continue  # an equity position, not an option
            contract = self._contract_or_none(symbol)
            if contract is None:
                continue
            action = OrderAction.SELL if position.quantity < 0 else OrderAction.BUY
            leg = OrderLeg(
                symbol=symbol,
                right=contract.right,
                strike=contract.strike,
                expiration=contract.expiration,
                action=action,
                quantity=abs(position.quantity),
            )
            groups.setdefault((contract.underlying, contract.expiration), []).append(leg)

        self._reserved_collateral = sum(self._required_collateral(legs) for legs in groups.values())

    def _contract_or_none(self, option_symbol: str) -> OptionContract | None:
        try:
            return self._find_contract(option_symbol)
        except NoQuoteAvailableError:
            return None

    # -------------------------------------------------------- settlement

    def settle_expiration(self, underlying_symbol: str, expiration: date, settlement_price: float) -> list[ExpirationSettlement]:
        """Cash- and share-settles every option position on
        `underlying_symbol` expiring on `expiration`, given the
        underlying's settlement price. Must be called explicitly (this
        simulator has no clock-driven background process) — a test
        harness or a future scheduler decides when "expiration" happens.
        """
        results: list[ExpirationSettlement] = []
        for symbol in list(self._positions.keys()):
            contract = self._contract_or_none(symbol)
            if contract is None or contract.underlying != underlying_symbol or contract.expiration != expiration:
                continue
            position = self._positions[symbol]
            intrinsic = (
                max(settlement_price - contract.strike, 0.0)
                if contract.right == OptionRight.CALL
                else max(contract.strike - settlement_price, 0.0)
            )
            was_itm = intrinsic > 0
            cash_impact = 0.0
            share_impact = 0
            assigned_or_exercised = False

            if was_itm:
                assigned_or_exercised = True
                qty = abs(position.quantity)
                is_short = position.quantity < 0
                buys_shares = (contract.right == OptionRight.PUT) == is_short
                # short put -> assigned -> buy shares; long put -> exercised -> sell shares
                # short call -> assigned -> sell shares; long call -> exercised -> buy shares
                share_delta = _CONTRACT_MULTIPLIER * qty * (1 if buys_shares else -1)
                cash_delta = -contract.strike * _CONTRACT_MULTIPLIER * qty * (1 if buys_shares else -1)
                self._cash += cash_delta
                cash_impact = cash_delta
                share_impact = share_delta
                self._update_position(underlying_symbol, share_delta, contract.strike, 1)

            self._positions.pop(symbol, None)
            results.append(
                ExpirationSettlement(
                    symbol=symbol,
                    right=contract.right,
                    strike=contract.strike,
                    expiration=expiration,
                    was_itm=was_itm,
                    assigned_or_exercised=assigned_or_exercised,
                    cash_impact=cash_impact,
                    share_impact=share_impact,
                )
            )

        self._recompute_collateral()
        return results
