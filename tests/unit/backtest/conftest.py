"""Shared fixtures for the backtest test suite."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.commissions import CommissionSchedule
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType

UNDERLYING = "XYZ"
EXPIRATION = date(2024, 2, 16)


@pytest.fixture
def zero_friction_config() -> PaperBrokerConfig:
    """MID, no commission, no slippage — isolates "what does the math
    say" from "what does execution cost," used by tests that care about
    payoff formulas rather than execution quality."""
    return PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.0, slippage_bps=0.0, spread_capture_fraction=0.0)


@pytest.fixture
def slippage_config() -> PaperBrokerConfig:
    return PaperBrokerConfig(
        fill_model=FillModel.MID_WITH_SLIPPAGE,
        commission_per_contract=0.65,
        slippage_bps=50.0,
        spread_capture_fraction=0.5,
        thin_volume_threshold=50,
        thin_open_interest_threshold=200,
    )


@pytest.fixture
def zero_commission_schedule() -> CommissionSchedule:
    return CommissionSchedule(per_contract=0.0, per_leg_base=0.0)


@pytest.fixture
def standard_commission_schedule() -> CommissionSchedule:
    return CommissionSchedule(per_contract=0.65, per_leg_base=0.0)


def make_quote(
    *,
    strike: float,
    right: OptionRight,
    bid: float,
    ask: float,
    quote_date: date = date(2024, 1, 2),
    expiration: date = EXPIRATION,
    underlying: str = UNDERLYING,
    volume: int = 500,
    open_interest: int = 1000,
    underlying_price: float = 100.0,
    iv: float | None = None,
) -> HistoricalOptionQuote:
    return HistoricalOptionQuote(
        underlying=underlying,
        quote_date=quote_date,
        expiration=expiration,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        underlying_price=underlying_price,
        iv=iv,
    )


def put_credit_spread_legs() -> tuple[BacktestLeg, ...]:
    return (
        BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),
        BacktestLeg(right=OptionRight.PUT, strike=90.0, side="buy"),
    )


def cash_secured_put_legs() -> tuple[BacktestLeg, ...]:
    return (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),)


def covered_call_legs() -> tuple[BacktestLeg, ...]:
    return (BacktestLeg(right=OptionRight.CALL, strike=105.0, side="sell"),)
