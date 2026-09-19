"""Tests for account/position/underlying-quote/option-chain retrieval —
verifying data is correctly normalized into src.data's canonical
schemas, including Greeks."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.brokers.ibkr import IBKRBroker, IBKRConfig
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from tests.unit.brokers.fakes import FakeIBClient, make_option_params


async def _connected_broker(**fake_kwargs) -> tuple[IBKRBroker, FakeIBClient]:
    fake = FakeIBClient(**fake_kwargs)
    broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
    await broker.connect()
    return broker, fake


class TestGetAccount:
    @pytest.mark.asyncio
    async def test_returns_canonical_account(self):
        broker, _ = await _connected_broker()
        account = await broker.get_account()
        assert account.account_id == "DU1234567"
        assert account.net_liquidation == 100_000.0
        assert account.buying_power == 200_000.0
        assert account.cash_balance == 50_000.0
        assert account.maintenance_margin == 1_500.0
        assert account.currency == "USD"
        assert account.source == "ibkr"


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_no_positions(self):
        broker, _ = await _connected_broker(positions=[])
        assert await broker.get_positions() == []

    @pytest.mark.asyncio
    async def test_returns_canonical_positions(self):
        raw_position = SimpleNamespace(contract=SimpleNamespace(symbol="AAPL", localSymbol=None), position=100, avgCost=220.0)
        broker, _ = await _connected_broker(positions=[raw_position])
        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 100
        assert positions[0].avg_cost == 220.0

    @pytest.mark.asyncio
    async def test_short_position_has_negative_quantity(self):
        raw_position = SimpleNamespace(contract=SimpleNamespace(symbol="AAPL", localSymbol=None), position=-1, avgCost=2.5)
        broker, _ = await _connected_broker(positions=[raw_position])
        positions = await broker.get_positions()
        assert positions[0].quantity == -1


class TestGetUnderlyingQuote:
    @pytest.mark.asyncio
    async def test_returns_canonical_quote(self):
        broker, _ = await _connected_broker()
        quote = await broker.get_underlying_quote("AAPL")
        assert isinstance(quote, UnderlyingQuote)
        assert quote.symbol == "AAPL"
        assert quote.bid == 224.9
        assert quote.ask == 225.1
        assert quote.source == "ibkr"


class TestGetOptionChain:
    @pytest.mark.asyncio
    async def test_returns_canonical_chain_with_contracts(self):
        option_params = {"AAPL": make_option_params(["20260320"], [200.0, 210.0, 220.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        assert isinstance(chain, OptionChain)
        assert chain.source == "ibkr"
        # 3 strikes x 2 rights (C/P) x 1 expiration = 6 contracts
        assert len(chain.contracts) == 6
        assert all(isinstance(c, OptionContract) for c in chain.contracts)

    @pytest.mark.asyncio
    async def test_contracts_carry_greeks_and_iv(self):
        option_params = {"AAPL": make_option_params(["20260320"], [220.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        contract = chain.contracts[0]
        assert contract.iv == pytest.approx(0.28)
        assert contract.delta == pytest.approx(-0.30)
        assert contract.gamma == pytest.approx(0.02)
        assert contract.theta == pytest.approx(-0.05)
        assert contract.vega == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_contracts_carry_underlying_price(self):
        option_params = {"AAPL": make_option_params(["20260320"], [220.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        assert chain.contracts[0].underlying_price == chain.underlying.mid

    @pytest.mark.asyncio
    async def test_expiration_correctly_parsed(self):
        option_params = {"AAPL": make_option_params(["20260320"], [220.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        assert chain.contracts[0].expiration == date(2026, 3, 20)

    @pytest.mark.asyncio
    async def test_no_option_params_returns_empty_chain(self):
        broker, _ = await _connected_broker(option_params={})
        chain = await broker.get_option_chain("AAPL")
        assert chain.contracts == []

    @pytest.mark.asyncio
    async def test_strikes_filtered_to_moneyness_band(self):
        # underlying mid is 225.0; a strike far outside the default 30%
        # band should be excluded from the chain.
        option_params = {"AAPL": make_option_params(["20260320"], [225.0, 1000.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        strikes = {c.strike for c in chain.contracts}
        assert 1000.0 not in strikes
        assert 225.0 in strikes

    @pytest.mark.asyncio
    async def test_raw_broker_response_never_returned_directly(self):
        # get_option_chain's return value must be exactly the canonical
        # type, never a raw ticker/contract object leaking through.
        option_params = {"AAPL": make_option_params(["20260320"], [220.0])}
        broker, _ = await _connected_broker(option_params=option_params)
        chain = await broker.get_option_chain("AAPL")
        assert type(chain) is OptionChain
        assert all(type(c) is OptionContract for c in chain.contracts)
