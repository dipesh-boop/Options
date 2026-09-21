"""Long call — new in Step 19A. A fully-funded, unlimited-upside
bullish debit position (never a naked short call)."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy the call at or below the target delta.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll up and out to lock in gains and reduce time decay exposure.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_long_call_position(*, strike: float, premium: float, contracts: int) -> Position:
    return Position(legs=[Leg(right=OptionRight.CALL, strike=strike, side=Side.BUY, entry_price=premium, quantity=contracts)])


def evaluate_long_call(
    *,
    ticker: str,
    expiration: date,
    call_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_long_call_position(strike=call_contract.strike, premium=call_contract.mid, contracts=num_contracts)
    capital = call_contract.mid * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.LONG_CALL, ticker=ticker, expiration=expiration, position=position,
        contracts=[call_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="bullish", volatility_outlook="neutral",
        required_positions="none, fully paid for at entry",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
