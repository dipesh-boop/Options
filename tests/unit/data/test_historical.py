"""Tests for canonical historical bars and the no-lookahead guard."""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.data.historical import HistoricalBar, HistoricalDataProvider, assert_no_lookahead


def _valid_bar_kwargs(**overrides) -> dict:
    base = dict(symbol="AAPL", bar_date=date(2026, 1, 15), open=224.0, high=226.5, low=223.0, close=225.0, volume=50_000_000, source="mock")
    base.update(overrides)
    return base


class TestValidBar:
    def test_constructs_successfully(self):
        bar = HistoricalBar(**_valid_bar_kwargs())
        assert bar.close == 225.0

    def test_is_frozen(self):
        bar = HistoricalBar(**_valid_bar_kwargs())
        with pytest.raises(ValidationError):
            bar.close = 999.0  # type: ignore[misc]

    def test_no_timestamp_field_required(self):
        # HistoricalBar deliberately has no live-data timestamp — its
        # validity is bar_date, not a fetch time.
        assert not hasattr(HistoricalBar, "timestamp") or "timestamp" not in HistoricalBar.model_fields


class TestInvalidBar:
    def test_high_below_low_rejected(self):
        with pytest.raises(ValidationError):
            HistoricalBar(**_valid_bar_kwargs(high=100.0, low=200.0))

    def test_open_outside_range_rejected(self):
        with pytest.raises(ValidationError):
            HistoricalBar(**_valid_bar_kwargs(open=300.0))

    def test_close_outside_range_rejected(self):
        with pytest.raises(ValidationError):
            HistoricalBar(**_valid_bar_kwargs(close=1.0))

    def test_negative_volume_rejected(self):
        with pytest.raises(ValidationError):
            HistoricalBar(**_valid_bar_kwargs(volume=-1))

    def test_non_positive_price_rejected(self):
        with pytest.raises(ValidationError):
            HistoricalBar(**_valid_bar_kwargs(open=0.0))

    def test_extra_field_rejected(self):
        payload = {**_valid_bar_kwargs(), "adjusted_close": 224.5}
        with pytest.raises(ValidationError):
            HistoricalBar.model_validate(payload)


class TestAssertNoLookahead:
    def test_all_bars_on_or_before_as_of_pass_through_unchanged(self):
        bars = [
            HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 1, 13))),
            HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 1, 14))),
            HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 1, 15))),
        ]
        result = assert_no_lookahead(bars, as_of=date(2026, 1, 15))
        assert result == bars

    def test_a_single_future_bar_raises(self):
        bars = [
            HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 1, 14))),
            HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 1, 16))),  # after as_of
        ]
        with pytest.raises(ValueError, match="lookahead"):
            assert_no_lookahead(bars, as_of=date(2026, 1, 15))

    def test_empty_bar_list_passes(self):
        assert assert_no_lookahead([], as_of=date(2026, 1, 15)) == []

    def test_error_message_names_the_offending_dates(self):
        bars = [HistoricalBar(**_valid_bar_kwargs(bar_date=date(2026, 2, 1)))]
        with pytest.raises(ValueError, match="2026-02-01"):
            assert_no_lookahead(bars, as_of=date(2026, 1, 15))


class _FakeHistoricalProvider(HistoricalDataProvider):
    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        return [HistoricalBar(**_valid_bar_kwargs(symbol=symbol, bar_date=start))]


class TestHistoricalDataProviderContract:
    @pytest.mark.asyncio
    async def test_concrete_provider_returns_canonical_bars(self):
        provider = _FakeHistoricalProvider()
        bars = await provider.get_bars("AAPL", date(2026, 1, 1), date(2026, 1, 31))
        assert all(isinstance(b, HistoricalBar) for b in bars)

    def test_cannot_instantiate_abstract_provider_directly(self):
        with pytest.raises(TypeError):
            HistoricalDataProvider()  # type: ignore[abstract]
