"""Shared canonical-model plumbing and the abstract provider interface
every concrete market data source (IBKR, Schwab, a mock, a historical
vendor) must implement.

Design principle this module enforces structurally, not just by
convention: a concrete provider's public methods return ONLY the
canonical types defined in this package (`quotes.py`, `option_chain.py`,
`historical.py`, `earnings.py`) — never a raw broker SDK object or a
bare dict. Every canonical type is a strict Pydantic model
(`extra="forbid"`, `frozen=True`), so even an attempt to pass a raw,
broker-shaped dict through as if it were already canonical fails
validation unless it has been explicitly, deliberately mapped field by
field.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from src.data.option_chain import OptionChain
    from src.data.quotes import UnderlyingQuote

# Placeholder policy default — TODO(Phase 0): move to config once a
# real risk/data config exists, the same way model routing moved to
# config/llm.yaml. src.llm.schemas.MAX_MARKET_DATA_AGE is a separate,
# independently-declared constant for the same underlying concern
# (TradeProposal-level market data freshness); the two should become one
# config value when that phase lands — flagged, not silently unified.
DEFAULT_MAX_QUOTE_AGE = timedelta(minutes=15)

# Step 22.7 (PAPER_TRADING_V1.4.6): a provider timestamp materially
# AHEAD of `as_of` means clock skew or malformed/corrupted provider
# data, never genuinely fresher data than could possibly have been
# captured. Left unguarded, `age = as_of - timestamp` goes negative and
# `age > max_age` never fires, so a corrupted future timestamp would
# sail through `freshness_status`/`require_fresh` as FRESH regardless
# of `max_age` -- this is the general "future timestamp" gap in the
# canonical freshness primitive itself (distinct from, and upstream of,
# `src.llm.schemas.TradeProposal`'s and `src.brokers.fidelity`'s own
# separate `data_timestamp > timestamp` checks, both of which already
# fail closed on this and are untouched here). This tolerance absorbs
# ordinary clock skew between this process and a provider's server
# without loosening `max_age` itself -- it only ever makes an existing
# freshness check *harder* to pass, never easier, and is far smaller
# than `DEFAULT_MAX_QUOTE_AGE` so it cannot become a de facto freshness
# window of its own.
_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=1)

# Well-known source identifiers concrete providers should use for
# consistency. `source` is a plain string field, not a closed enum,
# because the historical-data vendor and any future data providers are
# still open questions (ARCHITECTURE.md §12) — locking the type now
# would force a premature choice.
SOURCE_IBKR = "ibkr"
SOURCE_SCHWAB = "schwab"
SOURCE_MOCK = "mock"


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"


class StaleDataError(RuntimeError):
    """Raised when data is older than the configured maximum age and a
    caller has asked this module to enforce that (`require_fresh`,
    `assert_tradable`). This is the actual "prohibit trade approval"
    mechanism — a `FreshnessStatus.STALE` value that nothing ever checks
    would just be documentation; this raises."""


class StrictModel(BaseModel):
    """Base for every canonical schema in src.data. `extra="forbid"`
    means a raw provider dict — which will almost always carry
    provider-specific fields this schema doesn't define — fails
    validation instead of being silently accepted as if it were already
    normalized. `frozen=True` means a canonical instance can't be mutated
    into something it wasn't after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TimestampedModel(StrictModel):
    """Shared base for every canonical type that carries a capture
    timestamp and a source: `UnderlyingQuote`, `OptionContract`,
    `OptionChain`, `EarningsEvent`. (`HistoricalBar` deliberately does
    NOT inherit this — a historical bar's relevant integrity property is
    point-in-time validity for backtesting, not live-data freshness; see
    historical.py.)"""

    timestamp: datetime
    source: str = Field(min_length=1, max_length=32)

    @field_validator("timestamp")
    @classmethod
    def _require_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    def age(self, as_of: datetime) -> timedelta:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of - self.timestamp

    def freshness_status(self, as_of: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE) -> FreshnessStatus:
        age = self.age(as_of)
        if age < -_MAX_FUTURE_CLOCK_SKEW:
            # A timestamp materially in the future is never FRESH, no
            # matter how negative `age` computes to -- see
            # `_MAX_FUTURE_CLOCK_SKEW`'s own comment.
            return FreshnessStatus.STALE
        return FreshnessStatus.STALE if age > max_age else FreshnessStatus.FRESH

    def require_fresh(self, as_of: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE):
        """Returns self if fresh; raises StaleDataError otherwise. This
        is the choke-point every future caller that would act on this
        data (screen it, price it, propose a trade from it) must pass
        through."""
        age = self.age(as_of)
        if age < -_MAX_FUTURE_CLOCK_SKEW:
            raise StaleDataError(
                f"{type(self).__name__} from {self.source!r} timestamped {self.timestamp.isoformat()} "
                f"is {-age} ahead of as_of={as_of.isoformat()}, exceeding the {_MAX_FUTURE_CLOCK_SKEW} "
                "clock-skew tolerance -- treated as an invalid/untrustworthy timestamp, never as fresh"
            )
        if age > max_age:
            raise StaleDataError(
                f"{type(self).__name__} from {self.source!r} timestamped {self.timestamp.isoformat()} "
                f"is {age} old, exceeding max allowed age of {max_age} (as_of={as_of.isoformat()})"
            )
        return self


_T = TypeVar("_T")


def ensure_canonical(obj: object, expected_type: type[_T]) -> _T:
    """Runtime boundary guard: accepts nothing but a genuine instance of
    `expected_type` (exact type, not `isinstance` — a subclass can't
    smuggle extra attributes past a caller that only checks
    `isinstance`), for the same reason
    `src.llm.schemas.ensure_trade_proposal` exists. Call this at any
    point where market data is about to flow toward a consumer that must
    never see a raw provider response — most importantly, before it
    reaches anything in src.llm."""
    if type(obj) is not expected_type:
        raise TypeError(
            f"Expected a validated {expected_type.__name__} instance, got {type(obj).__name__!r}. "
            f"Raw provider responses must never cross this boundary."
        )
    return obj


class ProviderError(RuntimeError):
    """Raised by a concrete provider for connection/auth/upstream
    failures. Distinct from StaleDataError — a ProviderError means "no
    usable data," a StaleDataError means "usable data that's too old to
    act on.\""""


class MarketDataProvider(ABC):
    """Every concrete market data source implements this. Every method's
    return type is a canonical schema — never a raw broker response."""

    @abstractmethod
    async def get_option_chain(self, symbol: str) -> "OptionChain":
        """Current option chain (all expiries within whatever window the
        provider fetches) plus the underlying quote, for `symbol`."""
        raise NotImplementedError

    @abstractmethod
    async def get_underlying_quote(self, symbol: str) -> "UnderlyingQuote":
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - default no-op
        """Release any held connections."""
        return None
