"""Portfolio state, as the Risk Engine needs to see it, and the
aggregation helpers built on top of it: capital deployed, cash reserve,
concentration inputs, and duplicate-position detection.

`Portfolio` is deliberately not owned by `src.brokers` — a broker's
`Account`/`Position` types (src.brokers.base) describe what one broker
reports; `Portfolio` is the Risk Engine's own aggregated view, which may
in practice be assembled from one or more brokers' reconciled state.
That assembly step lives outside this module (not implemented yet); this
module only defines the shape the Risk Engine consumes and the pure
functions that read it.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.llm.schemas import StrategyType


def _check_finite(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    return value


class PortfolioPositionLeg(BaseModel):
    """Enough of one existing leg to reprice it under a stress scenario
    (src.risk.stress) — a minimal, position-focused echo of
    `src.quant.black_scholes.Leg`, kept independent of that dataclass so
    `src.risk` owns its own portfolio-state schema."""

    model_config = ConfigDict(frozen=True)

    right: Literal["C", "P"]
    side: Literal["buy", "sell"]
    strike: float = Field(gt=0)
    entry_price: float = Field(ge=0)


class PortfolioPosition(BaseModel):
    """One currently-open position, as tracked by the Risk Engine.
    `max_loss`/`capital_at_risk` are values already computed by Python
    Quant at entry — this module does not recompute them for existing
    positions, only for the new proposal under review
    (see `src.risk.trade_risk`)."""

    model_config = ConfigDict(frozen=True)

    position_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(pattern=r"^[A-Z]{1,10}$")
    sector: str = Field(min_length=1, max_length=64)
    strategy: StrategyType
    expiration: date
    legs: list[PortfolioPositionLeg] = Field(min_length=1, max_length=4)
    contracts: int = Field(gt=0)
    underlying_shares: int = Field(ge=0, default=0)
    capital_at_risk: float = Field(ge=0)
    max_loss: float = Field(ge=0)
    opened_at: datetime

    @property
    def strikes(self) -> frozenset[float]:
        return frozenset(leg.strike for leg in self.legs)

    @field_validator("capital_at_risk", "max_loss")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v, "position economics")

    @field_validator("opened_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        return v


class UnderlyingHolding(BaseModel):
    """Shares of stock already held, separate from the option positions
    in `Portfolio.positions`. A covered call's collateral (100 shares
    per contract) and its cost basis (needed to compute max
    profit/loss/breakeven via `src.quant.expected_value.covered_call_economics`)
    both come from here — neither is ever inferable from a
    `TradeProposal` or from market data."""

    model_config = ConfigDict(frozen=True)

    shares: int = Field(gt=0)
    cost_basis: float = Field(gt=0)

    @field_validator("cost_basis")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v, "cost_basis")


class Portfolio(BaseModel):
    """The Risk Engine's aggregated view of the account(s) it protects.
    `peak_equity` is the historical high-water mark used for drawdown
    (src.risk.drawdown) — it is supplied, not derived, since only a
    persisted ledger (not implemented yet) can know the true all-time
    high; a `peak_equity` lower than `nav` is a caller error, not
    something this module silently corrects."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    account_alias: str | None = None
    nav: float = Field(gt=0)
    cash: float = Field(ge=0)
    peak_equity: float = Field(gt=0)
    positions: list[PortfolioPosition] = Field(default_factory=list)
    underlying_holdings: dict[str, UnderlyingHolding] = Field(default_factory=dict)
    sector_by_ticker: dict[str, str] = Field(default_factory=dict)
    price_history: dict[str, list[float]] = Field(default_factory=dict)
    halted: bool = False
    halt_reason: str | None = None

    @field_validator("nav", "cash", "peak_equity")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _check_finite(v, "portfolio value")

    @field_validator("as_of")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _validate(self) -> "Portfolio":
        if self.cash > self.nav:
            raise ValueError(f"cash ({self.cash}) cannot exceed nav ({self.nav})")
        if self.peak_equity < self.nav:
            raise ValueError(
                f"peak_equity ({self.peak_equity}) cannot be less than current nav ({self.nav})"
            )
        if self.halted and not self.halt_reason:
            raise ValueError("halt_reason is required when halted is True")
        return self


def total_capital_at_risk(portfolio: Portfolio) -> float:
    return sum(p.capital_at_risk for p in portfolio.positions)


def capital_deployed_pct(portfolio: Portfolio) -> float:
    """Fraction of NAV currently tied up as capital across open
    positions (cash no longer freely available)."""
    deployed = portfolio.nav - portfolio.cash
    return deployed / portfolio.nav


def cash_reserve_pct(portfolio: Portfolio) -> float:
    return portfolio.cash / portfolio.nav


def underlying_exposure_pct(portfolio: Portfolio, ticker: str, additional_capital_at_risk: float = 0.0) -> float:
    existing = sum(p.capital_at_risk for p in portfolio.positions if p.ticker == ticker)
    return (existing + additional_capital_at_risk) / portfolio.nav


def sector_exposure_pct(portfolio: Portfolio, sector: str, additional_capital_at_risk: float = 0.0) -> float:
    existing = sum(
        p.capital_at_risk
        for p in portfolio.positions
        if portfolio.sector_by_ticker.get(p.ticker, "UNKNOWN") == sector
    )
    return (existing + additional_capital_at_risk) / portfolio.nav


def find_duplicate_position(
    portfolio: Portfolio, *, ticker: str, strategy: StrategyType, expiration: date, strikes: frozenset[float]
) -> PortfolioPosition | None:
    """A position is a duplicate if it already exists on the same
    underlying, strategy, expiration, and exact strike set — not merely
    "another position in the same ticker," which the underlying
    concentration check already governs separately."""
    for p in portfolio.positions:
        if (
            p.ticker == ticker
            and p.strategy == strategy
            and p.expiration == expiration
            and p.strikes == strikes
        ):
            return p
    return None
