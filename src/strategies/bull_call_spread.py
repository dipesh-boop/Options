"""Bull call spread — new in Step 19A. A defined-risk, fully-paid-for
bullish debit structure."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy the lower-strike call and sell the higher-strike call in one combo order for a net debit.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("No adjustment beyond close/roll; the debit paid is already the full defined risk.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_bull_call_spread_position(
    *, long_strike: float, long_premium: float, short_strike: float, short_premium: float, contracts: int
) -> Position:
    return Position(legs=[
        Leg(right=OptionRight.CALL, strike=long_strike, side=Side.BUY, entry_price=long_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=short_strike, side=Side.SELL, entry_price=short_premium, quantity=contracts),
    ])


def evaluate_bull_call_spread(
    *,
    ticker: str,
    expiration: date,
    long_call_contract: OptionContract,
    short_call_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_bull_call_spread_position(
        long_strike=long_call_contract.strike, long_premium=long_call_contract.mid,
        short_strike=short_call_contract.strike, short_premium=short_call_contract.mid, contracts=num_contracts,
    )
    debit = long_call_contract.mid - short_call_contract.mid
    capital = max(debit, 0.0) * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.BULL_CALL_SPREAD, ticker=ticker, expiration=expiration, position=position,
        contracts=[long_call_contract, short_call_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="bullish", volatility_outlook="neutral",
        required_positions="none, fully paid for at entry",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
