"""Backtest position/trade data model and the day-by-day simulation
loop that manages open positions through to close, expiration, or
assignment.

`HistoricalOptionQuote` is this package's own canonical historical
options-chain quote — deliberately not `src.data.option_chain
.OptionContract` (a *live* quote with `TimestampedModel` freshness
semantics that make no sense for backtesting: a 2024-03-15 quote is
exactly as valid today as it was the day after) and deliberately not
built by inventing a new shared type either — the same reasoning
`src.data.historical.HistoricalBar`'s docstring already gives for why a
historical type stays independent of the live one. This is the
options-chain analogue `ARCHITECTURE.md §12` flagged as not yet decided
for a real *vendor* — this module only defines the shape a vendor would
need to fill, exactly like `HistoricalDataProvider` already does for
underlying bars.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType


class LookaheadViolationError(ValueError):
    """Raised the moment simulation code is about to use data dated
    after the simulated `as_of` date — the concrete guard behind "no
    look-ahead bias," reusing `src.data.historical.assert_no_lookahead`
    for underlying bars and applying the identical rule to option
    quotes, which that function doesn't cover."""


@dataclass(frozen=True)
class HistoricalOptionQuote:
    underlying: str
    quote_date: date
    expiration: date
    strike: float
    right: OptionRight
    bid: float
    ask: float
    volume: int
    open_interest: int
    underlying_price: float
    iv: float | None = None

    def __post_init__(self) -> None:
        if self.bid < 0 or self.ask < 0:
            raise ValueError("bid/ask cannot be negative")
        if self.bid > self.ask:
            raise ValueError(f"bid ({self.bid}) cannot exceed ask ({self.ask})")
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.underlying_price <= 0:
            raise ValueError("underlying_price must be positive")

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return 0.0
        return round((self.bid + self.ask) / 2.0, 4)


class HistoricalOptionChainProvider(ABC):
    """Every concrete historical options-data source implements this.
    Returns `HistoricalOptionQuote` only — never a raw vendor response,
    same rule `src.data.provider.MarketDataProvider` enforces for live
    data."""

    @abstractmethod
    async def get_chain(self, underlying: str, as_of: date) -> list[HistoricalOptionQuote]:
        raise NotImplementedError


def assert_no_lookahead_options(quotes: list[HistoricalOptionQuote], as_of: date) -> list[HistoricalOptionQuote]:
    """The options-chain equivalent of
    `src.data.historical.assert_no_lookahead` — same rule, same
    exception-shaped contract (raises rather than returning a flag a
    caller could ignore), applied to `HistoricalOptionQuote` instead of
    `HistoricalBar` since that function is typed specifically for bars."""
    future = [q for q in quotes if q.quote_date > as_of]
    if future:
        offending = sorted({q.quote_date.isoformat() for q in future})
        raise LookaheadViolationError(
            f"lookahead violation: {len(future)} option quote(s) dated after as_of={as_of.isoformat()}: {offending}"
        )
    return quotes


BacktestSide = Literal["buy", "sell"]
"""Deliberately a plain string literal, not a new Enum — this package
spells sides the same way `src.brokers.base.OrderAction` does
("buy"/"sell"), so a leg converts to/from a broker order leg without a
translation table."""


@dataclass(frozen=True)
class BacktestLeg:
    """A leg's shape only — `right`/`strike`/`side`. Entry economics
    (what was actually paid/received) live on `BacktestPosition` as
    whole-position dollar totals instead of per-leg per-share prices:
    once a position is open, "what this leg's premium was" is never
    needed again on its own — only the position's total entry credit/
    debit is, for P&L — and a closing order's price never depends on
    the entry price at all (only the current quote does), so keeping it
    per-leg would just be a second, easier-to-drift place for the same
    number to live."""

    right: OptionRight
    strike: float
    side: BacktestSide


@dataclass(frozen=True)
class BacktestPosition:
    position_id: str
    ticker: str
    strategy: StrategyType
    legs: tuple[BacktestLeg, ...]
    contracts: int
    expiration: date
    opened_at: date
    management_dte: int
    profit_target_pct: float  # fraction of max profit at which to close early, e.g. 0.5
    realistic_entry_credit_total: float  # dollars, whole position, already x100 x contracts
    theoretical_entry_credit_total: float
    capital_at_risk: float  # dollars — this platform's simplified, strategy-level collateral estimate (see engine.py)
    entry_spread_pct: float = 0.0  # carried until close, same reason as entry_commission
    entry_commission: float = 0.0  # carried until close, so the eventual TradeRecord reports total (entry+exit) commission
    underlying_shares_held: int = 0  # covered call only: shares already owned before this leg was sold
    underlying_cost_basis: float = 0.0


@dataclass(frozen=True)
class TradeRecord:
    """One fully closed round trip — a filled entry through to a close,
    expiration, or assignment. `realistic_pnl`/`theoretical_pnl` are
    reported side by side for every single trade, not just in aggregate,
    so "theoretical midpoint return" vs. "realistic execution return"
    can be reconstructed and audited trade by trade."""

    position_id: str
    ticker: str
    strategy: StrategyType
    contracts: int
    opened_at: date
    closed_at: date
    close_reason: str  # "profit_target" | "dte_management" | "expiration_otm" | "assignment" | "exercise"
    capital_at_risk: float
    realistic_entry_credit: float
    realistic_exit_debit: float
    theoretical_entry_credit: float
    theoretical_exit_debit: float
    commission_paid: float
    realistic_pnl: float
    theoretical_pnl: float
    entry_spread_pct: float  # the widest leg's bid/ask spread (as a fraction of its mid) at entry

    @property
    def holding_period_days(self) -> int:
        return (self.closed_at - self.opened_at).days


@dataclass(frozen=True)
class EntrySignal:
    """One intended trade entry, already fully specified — this package
    executes and manages signals realistically; it does not generate
    them (no Strategy Screener exists yet in this codebase to generate
    one from). `legs`' `entry_price`/`theoretical_entry_price` are
    ignored on input (execution fills them from the actual quote) —
    only `right`/`strike`/`side` matter here."""

    ticker: str
    strategy: StrategyType
    legs: tuple[BacktestLeg, ...]
    expiration: date
    entry_date: date
    contracts_requested: int
    limit_price: float
    management_dte: int
    profit_target_pct: float
    underlying_shares_held: int = 0
    underlying_cost_basis: float = 0.0


@dataclass
class PortfolioState:
    """Mutable simulation state — unlike `src.risk.portfolio_risk
    .Portfolio` (frozen, gate-facing), this is the backtest engine's own
    fast-iterating working state; it is converted to trade
    records/equity points as it goes, never itself the final report."""

    realistic_cash: float
    theoretical_cash: float
    open_positions: list[BacktestPosition] = field(default_factory=list)
    closed_trades: list[TradeRecord] = field(default_factory=list)
    realistic_equity_curve: list[tuple[date, float]] = field(default_factory=list)
    theoretical_equity_curve: list[tuple[date, float]] = field(default_factory=list)
