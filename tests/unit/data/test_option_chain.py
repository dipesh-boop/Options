"""Tests for the canonical OptionContract/OptionChain schemas and the
freshness-based trade-approval gate."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.data.option_chain import OptionChain, OptionContract, OptionRight, assert_tradable
from src.data.provider import DEFAULT_MAX_QUOTE_AGE, StaleDataError
from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
EXPIRY = date(2026, 3, 20)


def _valid_contract_kwargs(**overrides) -> dict:
    base = dict(
        underlying="AAPL",
        option_symbol="AAPL260320P00220000",
        expiration=EXPIRY,
        strike=220.0,
        right=OptionRight.PUT,
        bid=2.40,
        ask=2.60,
        last=2.50,
        volume=500,
        open_interest=2000,
        iv=0.28,
        delta=-0.30,
        gamma=0.02,
        theta=-0.05,
        vega=0.15,
        underlying_price=225.0,
        timestamp=NOW,
        source="mock",
    )
    base.update(overrides)
    return base


class TestValidContract:
    def test_constructs_successfully(self):
        c = OptionContract(**_valid_contract_kwargs())
        assert c.underlying == "AAPL"
        assert c.right == OptionRight.PUT

    def test_all_required_fields_present(self):
        c = OptionContract(**_valid_contract_kwargs())
        for field in (
            "underlying", "option_symbol", "expiration", "strike", "right",
            "bid", "ask", "last", "volume", "open_interest",
            "iv", "delta", "gamma", "theta", "vega",
            "underlying_price", "timestamp", "source",
        ):
            assert hasattr(c, field)

    def test_greeks_and_iv_are_optional(self):
        kwargs = _valid_contract_kwargs()
        for field in ("iv", "delta", "gamma", "theta", "vega"):
            kwargs[field] = None
        c = OptionContract(**kwargs)
        assert c.iv is None
        assert c.delta is None

    def test_mid_is_bid_ask_average(self):
        c = OptionContract(**_valid_contract_kwargs(bid=2.0, ask=3.0))
        assert c.mid == 2.5

    def test_mid_falls_back_to_last(self):
        c = OptionContract(**_valid_contract_kwargs(bid=0.0, ask=0.0, last=2.75))
        assert c.mid == 2.75

    def test_is_frozen(self):
        c = OptionContract(**_valid_contract_kwargs())
        with pytest.raises(ValidationError):
            c.strike = 999.0  # type: ignore[misc]


class TestInvalidContract:
    def test_bid_above_ask_rejected(self):
        with pytest.raises(ValidationError, match="bid"):
            OptionContract(**_valid_contract_kwargs(bid=3.0, ask=2.0))

    def test_negative_strike_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**_valid_contract_kwargs(strike=-1.0))

    def test_zero_underlying_price_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**_valid_contract_kwargs(underlying_price=0.0))

    def test_invalid_right_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract.model_validate({**_valid_contract_kwargs(), "right": "X"})

    def test_delta_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**_valid_contract_kwargs(delta=1.5))

    def test_negative_open_interest_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**_valid_contract_kwargs(open_interest=-1))

    def test_blank_option_symbol_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**_valid_contract_kwargs(option_symbol=""))


class TestRawBrokerResponseNeverPassesAsCanonical:
    """A raw broker payload will almost always carry extra,
    provider-specific fields (internal ids, exchange codes, raw enum
    codes, ...) — extra="forbid" means those must be explicitly stripped
    and mapped by the provider adapter; they cannot leak through."""

    def test_conid_style_broker_field_rejected(self):
        raw = {**_valid_contract_kwargs(), "conid": 265598, "exchange": "SMART", "secType": "OPT"}
        with pytest.raises(ValidationError):
            OptionContract.model_validate(raw)

    def test_raw_broker_greeks_wrapper_rejected(self):
        # e.g. a raw IBKR-style nested "modelGreeks" blob instead of the
        # flat, normalized delta/gamma/theta/vega fields.
        kwargs = {k: v for k, v in _valid_contract_kwargs().items() if k not in ("delta", "gamma", "theta", "vega")}
        raw = {**kwargs, "modelGreeks": {"delta": -0.3, "gamma": 0.02}}
        with pytest.raises(ValidationError):
            OptionContract.model_validate(raw)


class TestOptionChain:
    def _underlying(self) -> UnderlyingQuote:
        return UnderlyingQuote(symbol="AAPL", bid=224.9, ask=225.1, last=225.0, volume=1_000_000, timestamp=NOW, source="mock")

    def test_constructs_with_contracts(self):
        contract = OptionContract(**_valid_contract_kwargs())
        chain = OptionChain(underlying=self._underlying(), contracts=[contract], timestamp=NOW, source="mock")
        assert len(chain.contracts) == 1

    def test_empty_contracts_list_allowed(self):
        chain = OptionChain(underlying=self._underlying(), contracts=[], timestamp=NOW, source="mock")
        assert chain.contracts == []

    def test_freshness_inherited_from_timestamped_model(self):
        chain = OptionChain(underlying=self._underlying(), contracts=[], timestamp=NOW, source="mock")
        fresh_check_time = NOW + timedelta(minutes=5)
        stale_check_time = NOW + timedelta(hours=2)
        assert chain.require_fresh(fresh_check_time) is chain
        with pytest.raises(StaleDataError):
            chain.require_fresh(stale_check_time)


class TestAssertTradable:
    def test_fresh_contract_passes(self):
        contract = OptionContract(**_valid_contract_kwargs())
        as_of = NOW + timedelta(minutes=5)
        assert assert_tradable(contract, as_of) is contract

    def test_stale_contract_prohibited(self):
        contract = OptionContract(**_valid_contract_kwargs())
        as_of = NOW + DEFAULT_MAX_QUOTE_AGE + timedelta(seconds=1)
        with pytest.raises(StaleDataError):
            assert_tradable(contract, as_of)

    def test_exactly_at_boundary_still_tradable(self):
        contract = OptionContract(**_valid_contract_kwargs())
        as_of = NOW + DEFAULT_MAX_QUOTE_AGE
        assert assert_tradable(contract, as_of) is contract

    def test_custom_max_age_respected(self):
        contract = OptionContract(**_valid_contract_kwargs())
        as_of = NOW + timedelta(minutes=2)
        with pytest.raises(StaleDataError):
            assert_tradable(contract, as_of, max_age=timedelta(minutes=1))

    def test_stale_contract_cannot_be_silently_used(self):
        # The core safety property: there is no code path that returns a
        # usable contract from assert_tradable when stale — it raises,
        # full stop, rather than returning a flagged-but-still-usable
        # object.
        contract = OptionContract(**_valid_contract_kwargs())
        as_of = NOW + timedelta(days=1)
        try:
            result = assert_tradable(contract, as_of)
            pytest.fail(f"expected StaleDataError, got a return value: {result!r}")
        except StaleDataError:
            pass
