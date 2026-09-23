"""Tests for the shared canonical-model plumbing: timezone-aware
timestamps, freshness status/enforcement, the ensure_canonical boundary
guard, and the abstract MarketDataProvider contract."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.data.option_chain import OptionChain, OptionContract
from src.data.provider import (
    DEFAULT_MAX_QUOTE_AGE,
    FreshnessStatus,
    MarketDataProvider,
    ProviderError,
    StaleDataError,
    TimestampedModel,
    ensure_canonical,
)
from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)


class _SampleTimestamped(TimestampedModel):
    value: int


class TestTimestampedModel:
    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _SampleTimestamped(value=1, timestamp=datetime(2026, 1, 15, 14, 30), source="mock")

    def test_aware_timestamp_accepted(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.timestamp == NOW

    def test_blank_source_rejected(self):
        with pytest.raises(ValidationError):
            _SampleTimestamped(value=1, timestamp=NOW, source="")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            _SampleTimestamped.model_validate({"value": 1, "timestamp": NOW, "source": "mock", "extra": "nope"})

    def test_is_frozen(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        with pytest.raises(ValidationError):
            obj.value = 2  # type: ignore[misc]


class TestAge:
    def test_age_is_the_elapsed_time(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.age(NOW + timedelta(minutes=10)) == timedelta(minutes=10)

    def test_naive_as_of_rejected(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        with pytest.raises(ValueError, match="timezone-aware"):
            obj.age(datetime(2026, 1, 15, 15, 0))


class TestFreshnessStatus:
    def test_within_max_age_is_fresh(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW + timedelta(minutes=14)) == FreshnessStatus.FRESH

    def test_exactly_at_max_age_is_fresh(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW + DEFAULT_MAX_QUOTE_AGE) == FreshnessStatus.FRESH

    def test_past_max_age_is_stale(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW + DEFAULT_MAX_QUOTE_AGE + timedelta(seconds=1)) == FreshnessStatus.STALE

    def test_custom_max_age_respected(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW + timedelta(minutes=2), max_age=timedelta(minutes=1)) == FreshnessStatus.STALE


class TestFutureTimestampDefensiveRule:
    """Step 22.7 (PAPER_TRADING_V1.4.6): a provider timestamp materially
    ahead of `as_of` must never be classified FRESH just because the
    naive `age = as_of - timestamp` computation goes negative."""

    def test_tiny_clock_skew_within_tolerance_is_still_fresh(self):
        # A few seconds of ordinary clock skew must not spuriously fail
        # otherwise-fresh data.
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW - timedelta(seconds=5)) == FreshnessStatus.FRESH

    def test_exactly_at_the_skew_tolerance_boundary_is_still_fresh(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW - timedelta(minutes=1)) == FreshnessStatus.FRESH

    def test_materially_future_timestamp_is_stale_not_fresh(self):
        # Without the defensive rule, age = as_of - timestamp = -1 hour,
        # and -1h > 15m is False, so this would incorrectly read FRESH.
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.freshness_status(NOW - timedelta(hours=1)) == FreshnessStatus.STALE

    def test_materially_future_timestamp_fails_require_fresh(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        with pytest.raises(StaleDataError, match="ahead of"):
            obj.require_fresh(NOW - timedelta(hours=1))

    def test_a_future_timestamp_does_not_bypass_require_fresh_via_a_huge_max_age(self):
        # Confirms the guard is independent of max_age -- widening
        # max_age must never rescue a future-dated timestamp.
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        with pytest.raises(StaleDataError):
            obj.require_fresh(NOW - timedelta(hours=1), max_age=timedelta(days=365))


class TestRequireFresh:
    def test_fresh_data_returns_self(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        assert obj.require_fresh(NOW + timedelta(minutes=1)) is obj

    def test_stale_data_raises(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="mock")
        with pytest.raises(StaleDataError):
            obj.require_fresh(NOW + timedelta(hours=1))

    def test_stale_data_error_message_is_informative(self):
        obj = _SampleTimestamped(value=1, timestamp=NOW, source="ibkr")
        with pytest.raises(StaleDataError, match="ibkr"):
            obj.require_fresh(NOW + timedelta(hours=1))


class TestEnsureCanonical:
    def _make_contract(self):
        return OptionContract(
            underlying="AAPL",
            option_symbol="AAPL260320P00220000",
            expiration=NOW.date() + timedelta(days=30),
            strike=220.0,
            right="P",
            bid=2.4,
            ask=2.6,
            last=2.5,
            volume=500,
            open_interest=2000,
            underlying_price=225.0,
            timestamp=NOW,
            source="mock",
        )

    def test_accepts_genuine_instance(self):
        contract = self._make_contract()
        assert ensure_canonical(contract, OptionContract) is contract

    def test_rejects_dict_with_correct_shape(self):
        contract = self._make_contract()
        raw = contract.model_dump()
        with pytest.raises(TypeError, match="Raw provider responses"):
            ensure_canonical(raw, OptionContract)

    def test_rejects_wrong_canonical_type(self):
        contract = self._make_contract()
        with pytest.raises(TypeError):
            ensure_canonical(contract, OptionChain)

    def test_rejects_subclass_instance(self):
        class SneakyContract(OptionContract):
            pass

        contract = SneakyContract(**self._make_contract().model_dump())
        with pytest.raises(TypeError):
            ensure_canonical(contract, OptionContract)

    def test_rejects_none(self):
        with pytest.raises(TypeError):
            ensure_canonical(None, OptionContract)


class _FakeProvider(MarketDataProvider):
    """Minimal concrete implementation, defined here purely to exercise
    the abstract base's contract — production adapters (IBKR, Schwab)
    are future work, not part of this change."""

    async def get_option_chain(self, symbol: str) -> OptionChain:
        quote = UnderlyingQuote(symbol=symbol, bid=99.0, ask=101.0, last=100.0, volume=1000, timestamp=NOW, source="mock")
        return OptionChain(underlying=quote, contracts=[], timestamp=NOW, source="mock")

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return UnderlyingQuote(symbol=symbol, bid=99.0, ask=101.0, last=100.0, volume=1000, timestamp=NOW, source="mock")


class TestMarketDataProviderContract:
    @pytest.mark.asyncio
    async def test_concrete_provider_returns_canonical_chain(self):
        provider = _FakeProvider()
        chain = await provider.get_option_chain("AAPL")
        assert ensure_canonical(chain, OptionChain) is chain

    @pytest.mark.asyncio
    async def test_concrete_provider_returns_canonical_quote(self):
        provider = _FakeProvider()
        quote = await provider.get_underlying_quote("AAPL")
        assert ensure_canonical(quote, UnderlyingQuote) is quote

    @pytest.mark.asyncio
    async def test_default_close_is_a_noop(self):
        provider = _FakeProvider()
        assert await provider.close() is None

    def test_cannot_instantiate_abstract_provider_directly(self):
        with pytest.raises(TypeError):
            MarketDataProvider()  # type: ignore[abstract]


def test_provider_error_is_a_runtime_error():
    assert issubclass(ProviderError, RuntimeError)
