"""Tests for src.portfolio.account_state: durable PaperBroker account state
and Portfolio persistence across process restarts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderType, PlaceOrderRequest
from src.brokers.paper import PaperBroker
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.portfolio.account_state import (
    InMemoryPaperAccountStateStore,
    InMemoryPortfolioStore,
    PaperAccountState,
    SqlitePaperAccountStateStore,
    SqlitePortfolioStore,
)
from src.risk.portfolio_risk import Portfolio

NOW = datetime.now(timezone.utc)
EXPIRATION = (NOW + timedelta(days=30)).date()


def _chain() -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=99.5, ask=100.5, last=100.0, volume=1_000_000, timestamp=NOW, source="test")
    contract = OptionContract(
        option_symbol="SPY_TEST_PUT", underlying="SPY", strike=5.0, expiration=EXPIRATION, right=OptionRight.PUT,
        bid=0.48, ask=0.52, last=0.5, volume=500, open_interest=1000, delta=-0.2, iv=0.22, underlying_price=100.0,
        timestamp=NOW, source="test",
    )
    return OptionChain(underlying=underlying, contracts=[contract], timestamp=NOW, source="test")


def _filled_broker() -> PaperBroker:
    broker = PaperBroker(initial_cash=100_000.0, account_id="acct-1", now=NOW)
    broker.update_market_data(_chain())
    request = PlaceOrderRequest(
        client_order_id="order-1",
        legs=[OrderLeg(symbol="SPY_TEST_PUT", right=OptionRight.PUT, strike=5.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1)],
        order_type=OrderType.LIMIT, limit_price=0.45,
    )
    asyncio.run(broker.place_order(request))
    return broker


class TestPaperBrokerStateRoundTrip:
    def test_export_then_restore_into_a_fresh_broker_preserves_account_state(self):
        broker = _filled_broker()
        state = broker.export_state()

        fresh = PaperBroker(initial_cash=0.0, account_id="acct-1", now=NOW)
        fresh.restore_state(state)

        original_account = asyncio.run(broker.get_account())
        fresh_account = asyncio.run(fresh.get_account())
        assert fresh_account.cash_balance == original_account.cash_balance
        assert asyncio.run(fresh.get_positions()) == asyncio.run(broker.get_positions())
        assert asyncio.run(fresh.get_fills()) == asyncio.run(broker.get_fills())

    def test_export_state_excludes_market_data_and_idempotency_state(self):
        broker = _filled_broker()
        state = broker.export_state()
        assert not hasattr(state, "chains")
        assert not hasattr(state, "underlyings")
        assert not hasattr(state, "idempotency")


class TestPaperAccountStateStore:
    @pytest.fixture(params=["memory", "sqlite"])
    def store(self, request, tmp_path):
        if request.param == "memory":
            return InMemoryPaperAccountStateStore()
        return SqlitePaperAccountStateStore(tmp_path / "account_state.db")

    def test_save_and_get_round_trip(self, store):
        state = _filled_broker().export_state()
        store.save(state)
        loaded = store.get("acct-1")
        assert loaded is not None
        assert loaded.cash == state.cash
        assert loaded.positions == state.positions

    def test_get_returns_none_for_unknown_account(self, store):
        assert store.get("does-not-exist") is None

    def test_save_is_replace_on_save_keyed_by_account_id(self, store):
        state1 = PaperAccountState(account_id="acct-1", cash=100.0, reserved_collateral=0.0)
        state2 = PaperAccountState(account_id="acct-1", cash=200.0, reserved_collateral=0.0)
        store.save(state1)
        store.save(state2)
        assert store.get("acct-1").cash == 200.0

    def test_sqlite_store_survives_a_fresh_instance_against_the_same_file(self, tmp_path):
        db_path = tmp_path / "account_state.db"
        state = _filled_broker().export_state()
        SqlitePaperAccountStateStore(db_path).save(state)

        reloaded = SqlitePaperAccountStateStore(db_path).get("acct-1")
        assert reloaded is not None
        assert reloaded.cash == state.cash
        assert reloaded.fills == state.fills


class TestPortfolioStore:
    @staticmethod
    def _portfolio() -> Portfolio:
        return Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)

    @pytest.fixture(params=["memory", "sqlite"])
    def store(self, request, tmp_path):
        if request.param == "memory":
            return InMemoryPortfolioStore()
        return SqlitePortfolioStore(tmp_path / "portfolio.db")

    def test_save_and_get_round_trip(self, store):
        portfolio = self._portfolio()
        store.save("acct-1", portfolio)
        loaded = store.get("acct-1")
        assert loaded is not None
        assert loaded.nav == portfolio.nav
        assert loaded.cash == portfolio.cash

    def test_get_returns_none_for_unknown_account(self, store):
        assert store.get("does-not-exist") is None

    def test_sqlite_store_survives_a_fresh_instance_against_the_same_file(self, tmp_path):
        db_path = tmp_path / "portfolio.db"
        SqlitePortfolioStore(db_path).save("acct-1", self._portfolio())

        reloaded = SqlitePortfolioStore(db_path).get("acct-1")
        assert reloaded is not None
        assert reloaded.nav == 100_000.0
