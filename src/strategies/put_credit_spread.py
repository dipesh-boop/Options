"""Put credit spread — retained unchanged from the platform's original
three strategies."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Sell the short put and buy the long put in one combo order for a net credit.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll the whole spread down and out if tested, preserving defined risk.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_put_credit_spread_position(
    *, short_strike: float, short_premium: float, long_strike: float, long_premium: float, contracts: int
) -> Position:
    return Position(legs=[
        Leg(right=OptionRight.PUT, strike=short_strike, side=Side.SELL, entry_price=short_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=long_strike, side=Side.BUY, entry_price=long_premium, quantity=contracts),
    ])


def evaluate_put_credit_spread(
    *,
    ticker: str,
    expiration: date,
    short_put_contract: OptionContract,
    long_put_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_put_credit_spread_position(
        short_strike=short_put_contract.strike, short_premium=short_put_contract.mid,
        long_strike=long_put_contract.strike, long_premium=long_put_contract.mid, contracts=num_contracts,
    )
    width = short_put_contract.strike - long_put_contract.strike
    credit = short_put_contract.mid - long_put_contract.mid
    capital = max(width - credit, 0.0) * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.PUT_CREDIT_SPREAD, ticker=ticker, expiration=expiration, position=position,
        contracts=[short_put_contract, long_put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="moderately_bullish", volatility_outlook="neutral",
        required_positions="none, defined-risk margin only",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
