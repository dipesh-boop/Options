"""Tests for the canonical earnings schema and the earnings-window check
the Strategy Screener relies on for the "no earnings-window entries"
universe rule."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.data.earnings import (
    EarningsCalendarProvider,
    EarningsEvent,
    EarningsTiming,
    is_within_earnings_window,
)
from src.data.provider import StaleDataError

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)


def _valid_kwargs(**overrides) -> dict:
    base = dict(symbol="AAPL", earnings_date=date(2026, 1, 29), timing=EarningsTiming.AFTER_MARKET, confirmed=True, timestamp=NOW, source="mock")
    base.update(overrides)
    return base


class TestValidEarningsEvent:
    def test_constructs_successfully(self):
        e = EarningsEvent(**_valid_kwargs())
        assert e.symbol == "AAPL"
        assert e.confirmed is True

    def test_timing_defaults_to_unknown(self):
        kwargs = _valid_kwargs()
        del kwargs["timing"]
        e = EarningsEvent(**kwargs)
        assert e.timing == EarningsTiming.UNKNOWN

    def test_confirmed_defaults_to_false(self):
        kwargs = _valid_kwargs()
        del kwargs["confirmed"]
        e = EarningsEvent(**kwargs)
        assert e.confirmed is False

    def test_is_frozen(self):
        e = EarningsEvent(**_valid_kwargs())
        with pytest.raises(ValidationError):
            e.confirmed = False  # type: ignore[misc]

    def test_calendar_entry_freshness_is_independent_of_earnings_date(self):
        # The earnings_date itself is fixed and doesn't get "stale" -
        # what can be stale is how long ago this calendar entry was
        # fetched/confirmed.
        e = EarningsEvent(**_valid_kwargs())
        assert e.require_fresh(NOW + timedelta(minutes=5)) is e
        with pytest.raises(StaleDataError):
            e.require_fresh(NOW + timedelta(days=1))


class TestInvalidEarningsEvent:
    def test_blank_symbol_rejected(self):
        with pytest.raises(ValidationError):
            EarningsEvent(**_valid_kwargs(symbol=""))

    def test_invalid_timing_rejected(self):
        with pytest.raises(ValidationError):
            EarningsEvent.model_validate({**_valid_kwargs(), "timing": "lunchtime"})

    def test_extra_field_rejected(self):
        payload = {**_valid_kwargs(), "analyst_estimate_eps": 2.35}
        with pytest.raises(ValidationError):
            EarningsEvent.model_validate(payload)


class TestIsWithinEarningsWindow:
    def test_expiration_on_earnings_date_is_within_window(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2026, 1, 29)) is True

    def test_expiration_just_inside_window_after_earnings(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2026, 2, 5), window_days=7) is True

    def test_expiration_just_outside_window_after_earnings(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2026, 2, 6), window_days=7) is False

    def test_expiration_before_earnings_within_window(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2026, 1, 25), window_days=7) is True

    def test_expiration_well_before_earnings_outside_window(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2025, 12, 1), window_days=7) is False

    def test_zero_window_only_matches_exact_date(self):
        event = EarningsEvent(**_valid_kwargs(earnings_date=date(2026, 1, 29)))
        assert is_within_earnings_window(event, expiration=date(2026, 1, 29), window_days=0) is True
        assert is_within_earnings_window(event, expiration=date(2026, 1, 30), window_days=0) is False

    def test_negative_window_rejected(self):
        event = EarningsEvent(**_valid_kwargs())
        with pytest.raises(ValueError):
            is_within_earnings_window(event, expiration=date(2026, 1, 29), window_days=-1)


class _FakeEarningsProvider(EarningsCalendarProvider):
    async def get_next_earnings(self, symbol: str) -> EarningsEvent | None:
        return EarningsEvent(**_valid_kwargs(symbol=symbol))


class TestEarningsCalendarProviderContract:
    @pytest.mark.asyncio
    async def test_concrete_provider_returns_canonical_event(self):
        provider = _FakeEarningsProvider()
        event = await provider.get_next_earnings("AAPL")
        assert isinstance(event, EarningsEvent)

    def test_cannot_instantiate_abstract_provider_directly(self):
        with pytest.raises(TypeError):
            EarningsCalendarProvider()  # type: ignore[abstract]
