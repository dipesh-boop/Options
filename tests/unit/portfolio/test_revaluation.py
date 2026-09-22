"""Tests for `src.portfolio.revaluation` (Step 22.4 Part 14)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.data.option_chain import OptionContract, OptionRight
from src.llm.schemas import StrategyType
from src.portfolio.revaluation import (
    PositionValuationStatus,
    build_contract_index,
    revalue_portfolio,
    revalue_position,
)
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
STALE = NOW - timedelta(minutes=30)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()


def _pcs_position(position_id="p1", ticker="SPY", contracts=2) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
        expiration=EXP,
        legs=[
            PortfolioPositionLeg(right="P", side="sell", strike=450.0, entry_price=6.0),
            PortfolioPositionLeg(right="P", side="buy", strike=440.0, entry_price=3.0),
        ],
        contracts=contracts, capital_at_risk=1400.0, max_loss=1400.0, opened_at=NOW - timedelta(days=5),
    )


def _contract(strike, right, bid, ask, *, timestamp=NOW) -> OptionContract:
    return OptionContract(
        underlying="SPY", option_symbol=f"SPY{EXP.isoformat()}{right}{strike:g}", expiration=EXP, strike=strike,
        right=OptionRight.PUT if right == "P" else OptionRight.CALL, bid=bid, ask=ask, last=(bid + ask) / 2,
        volume=100, open_interest=500, underlying_price=455.0, timestamp=timestamp, source="tradier",
    )


class TestRevaluePosition:
    def test_computes_unrealized_pnl_correctly(self):
        pos = _pcs_position()
        short = _contract(450.0, "P", 4.5, 4.7)
        long = _contract(440.0, "P", 1.8, 2.0)
        idx = build_contract_index([short, long])
        val = revalue_position(pos, underlying_price=455.0, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.status == PositionValuationStatus.OK
        # short: sold at 6.0, marks at 4.6 mid -> +1.4/share profit; long: bought at 3.0, marks at 1.9 -> -1.1/share
        # net = (1.4 - 1.1) * 2 contracts * 100 = 60
        assert abs(val.unrealized_pnl - 60.0) < 0.01

    def test_greeks_computed_when_iv_solvable(self):
        pos = _pcs_position()
        short = _contract(450.0, "P", 4.5, 4.7)
        long = _contract(440.0, "P", 1.8, 2.0)
        idx = build_contract_index([short, long])
        val = revalue_position(pos, underlying_price=455.0, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.delta is not None and val.gamma is not None

    def test_missing_leg_contract_is_data_insufficient_never_fabricated(self):
        pos = _pcs_position()
        short = _contract(450.0, "P", 4.5, 4.7)
        idx = build_contract_index([short])  # long leg missing
        val = revalue_position(pos, underlying_price=455.0, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.status == PositionValuationStatus.DATA_INSUFFICIENT
        assert val.unrealized_pnl is None

    def test_stale_leg_is_data_insufficient(self):
        pos = _pcs_position()
        short = _contract(450.0, "P", 4.5, 4.7)
        long = _contract(440.0, "P", 1.8, 2.0, timestamp=STALE)
        idx = build_contract_index([short, long])
        val = revalue_position(pos, underlying_price=455.0, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.status == PositionValuationStatus.DATA_INSUFFICIENT

    def test_missing_underlying_price_is_data_insufficient(self):
        pos = _pcs_position()
        idx = build_contract_index([_contract(450.0, "P", 4.5, 4.7), _contract(440.0, "P", 1.8, 2.0)])
        val = revalue_position(pos, underlying_price=None, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.status == PositionValuationStatus.DATA_INSUFFICIENT

    def test_dte_computed_from_expiration(self):
        pos = _pcs_position()
        idx = build_contract_index([_contract(450.0, "P", 4.5, 4.7), _contract(440.0, "P", 1.8, 2.0)])
        val = revalue_position(pos, underlying_price=455.0, contract_index=idx, as_of=NOW, rate=LIMITS.risk_free_rate)
        assert val.dte == (EXP - NOW.date()).days


class TestRevaluePortfolio:
    def test_isolates_data_insufficient_position_from_aggregate(self):
        pos1 = _pcs_position(position_id="p1")
        pos2 = _pcs_position(position_id="p2", ticker="QQQ")
        portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1, pos2])
        result = revalue_portfolio(
            portfolio, underlying_prices={"SPY": 455.0},
            contracts_by_ticker={"SPY": [_contract(450.0, "P", 4.5, 4.7), _contract(440.0, "P", 1.8, 2.0)]},
            limits=LIMITS, as_of=NOW,
        )
        assert result.is_complete is False
        assert result.positions_data_insufficient == ("p2",)
        assert result.total_unrealized_pnl_known is not None  # p1's pnl still counted

    def test_all_positions_ok_gives_complete_aggregate(self):
        pos1 = _pcs_position(position_id="p1")
        pos2 = _pcs_position(position_id="p2")
        portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1, pos2])
        contracts = [_contract(450.0, "P", 4.5, 4.7), _contract(440.0, "P", 1.8, 2.0)]
        result = revalue_portfolio(
            portfolio, underlying_prices={"SPY": 455.0}, contracts_by_ticker={"SPY": contracts}, limits=LIMITS, as_of=NOW,
        )
        assert result.is_complete is True
        assert abs(result.total_unrealized_pnl_known - 120.0) < 0.01

    def test_empty_portfolio_handled_cleanly(self):
        portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=100000.0, peak_equity=100000.0)
        result = revalue_portfolio(portfolio, underlying_prices={}, contracts_by_ticker={}, limits=LIMITS, as_of=NOW)
        assert result.total_unrealized_pnl_known is None
        assert result.is_complete is True

    def test_reuses_existing_drawdown_and_capital_helpers(self):
        portfolio = Portfolio(as_of=NOW, nav=90000.0, cash=90000.0, peak_equity=100000.0)
        result = revalue_portfolio(portfolio, underlying_prices={}, contracts_by_ticker={}, limits=LIMITS, as_of=NOW)
        assert abs(result.current_drawdown_pct - 0.10) < 1e-9
        assert result.cash_reserve_pct == 1.0
