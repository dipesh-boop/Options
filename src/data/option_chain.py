"""The canonical OptionContract schema — the single shape every
broker/data provider must convert its raw response into before any other
part of this codebase (screening, quant, the agent layer) ever sees it.

`iv`/`delta`/`gamma`/`theta`/`vega` on `OptionContract` are the values
*as reported by the data source* (a broker's own model, typically) —
they are reference data, not an authoritative calculation. Python Quant
(src.quant) independently recomputes greeks/IV from price when it
actually matters for a risk decision; it never trusts a provider's
stated numbers any more than it trusts an LLM's. That's why
`src/data` has no dependency on `src/quant`: this layer's job is
normalizing what a provider said, not computing anything itself.

A small, deliberate duplication: this module defines its own
`OptionRight` (CALL/PUT) rather than importing `src.quant.black_scholes
.OptionRight` or `src.llm.schemas.OptionRight` — three tiny copies of
the same enum now exist, one per layer, so each layer stays importable
and testable without cross-layer coupling. A shared `src/core/`
primitives module would be the natural Phase 0 cleanup (flagged in
progress.md), not done here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import Field, model_validator

from src.data.provider import DEFAULT_MAX_QUOTE_AGE, TimestampedModel
from src.data.quotes import UnderlyingQuote


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class OptionContract(TimestampedModel):
    """The canonical option contract. Every field a provider adapter
    must populate by explicitly mapping its raw response — `extra=
    "forbid"` (inherited via TimestampedModel/StrictModel) means a raw
    dict with provider-specific extra fields fails validation rather
    than silently passing through as if it were already normalized."""

    underlying: str = Field(min_length=1, max_length=10)
    option_symbol: str = Field(min_length=1, max_length=64)
    expiration: date
    strike: float = Field(gt=0)
    right: OptionRight

    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    last: float = Field(ge=0)
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)

    # Provider-reported reference values — see module docstring. None
    # when the provider doesn't supply a given greek/IV for this quote.
    iv: float | None = Field(default=None, ge=0)
    delta: float | None = Field(default=None, ge=-1, le=1)
    gamma: float | None = Field(default=None, ge=0)
    theta: float | None = None
    vega: float | None = Field(default=None, ge=0)

    # Step 22.4: additional per-quote provenance/liquidity fields a
    # richer provider (Tradier) can supply and Alpaca/mock/ibkr cannot
    # -- every one optional and additive, so an existing provider that
    # never sets them stays valid exactly as before. `bid_timestamp`/
    # `ask_timestamp` are the provider's own per-side quote times (may
    # differ from `timestamp`, the overall contract's own capture time,
    # e.g. a stale ask sitting under a fresher bid); `trade_timestamp`
    # is when `last` itself printed. Missing means missing -- never
    # backfilled from `timestamp`.
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    bid_timestamp: datetime | None = None
    ask_timestamp: datetime | None = None
    trade_timestamp: datetime | None = None

    underlying_price: float = Field(gt=0)

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return self.last
        return round((self.bid + self.ask) / 2, 4)

    @model_validator(mode="after")
    def _bid_not_above_ask(self) -> "OptionContract":
        if self.bid > 0 and self.ask > 0 and self.bid > self.ask:
            raise ValueError(f"bid ({self.bid}) cannot exceed ask ({self.ask})")
        return self

    @model_validator(mode="after")
    def _optional_timestamps_tz_aware(self) -> "OptionContract":
        for name in ("bid_timestamp", "ask_timestamp", "trade_timestamp"):
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self


class OptionChain(TimestampedModel):
    """A single fetch snapshot: an underlying quote plus its option
    contracts. Immutable and, like every canonical type, only ever
    constructed by an explicit provider-side mapping from a raw
    response — never assembled ad hoc from unvalidated data.
    `timestamp`/`source` and freshness checking (`freshness_status`,
    `require_fresh`) are inherited from `TimestampedModel`."""

    underlying: UnderlyingQuote
    contracts: list[OptionContract]


def assert_tradable(
    contract: OptionContract, as_of: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE
) -> OptionContract:
    """The single choke-point a future Strategy Screener / TradeProposal
    builder must call before treating a contract as tradable. Raises
    StaleDataError — not just a FreshnessStatus a caller could ignore —
    if the contract's data is older than `max_age` relative to `as_of`.
    This is the concrete "prohibit trade approval" mechanism."""
    return contract.require_fresh(as_of, max_age)
