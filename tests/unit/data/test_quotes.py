"""Tests for the canonical UnderlyingQuote schema."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)


def _valid_kwargs(**overrides) -> dict:
    base = dict(symbol="AAPL", bid=224.9, ask=225.1, last=225.0, volume=1_000_000, timestamp=NOW, source="mock")
    base.update(overrides)
    return base


class TestValidQuote:
    def test_constructs_successfully(self):
        q = UnderlyingQuote(**_valid_kwargs())
        assert q.symbol == "AAPL"

    def test_mid_is_bid_ask_average(self):
        q = UnderlyingQuote(**_valid_kwargs(bid=100.0, ask=102.0))
        assert q.mid == 101.0

    def test_mid_falls_back_to_last_when_no_bid_ask(self):
        q = UnderlyingQuote(**_valid_kwargs(bid=0.0, ask=0.0, last=225.5))
        assert q.mid == 225.5

    def test_is_frozen(self):
        q = UnderlyingQuote(**_valid_kwargs())
        with pytest.raises(ValidationError):
            q.last = 999.0  # type: ignore[misc]


class TestInvalidQuote:
    def test_negative_bid_rejected(self):
        with pytest.raises(ValidationError):
            UnderlyingQuote(**_valid_kwargs(bid=-1.0))

    def test_negative_volume_rejected(self):
        with pytest.raises(ValidationError):
            UnderlyingQuote(**_valid_kwargs(volume=-1))

    def test_blank_symbol_rejected(self):
        with pytest.raises(ValidationError):
            UnderlyingQuote(**_valid_kwargs(symbol=""))

    def test_extra_field_rejected(self):
        payload = {**_valid_kwargs(), "broker_internal_id": "xyz"}
        with pytest.raises(ValidationError):
            UnderlyingQuote.model_validate(payload)

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            UnderlyingQuote(**_valid_kwargs(timestamp=datetime(2026, 1, 15, 14, 30)))
