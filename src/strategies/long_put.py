"""Long put — new in Step 19A. A fully-funded, defined-risk (stock
floors at 0) bearish debit position, distinct from a protective put in
that it carries no existing share position to hedge."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy the put at or below the target delta.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll down and out to lock in gains and reduce time decay exposure.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_long_put_position(*, strike: float, premium: float, contracts: int) -> Position:
    return Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.BUY, entry_price=premium, quantity=contracts)])


def evaluate_long_put(
    *,
    ticker: str,
    expiration: date,
    put_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_long_put_position(strike=put_contract.strike, premium=put_contract.mid, contracts=num_contracts)
    capital = put_contract.mid * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.LONG_PUT, ticker=ticker, expiration=expiration, position=position,
        contracts=[put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="bearish", volatility_outlook="neutral",
        required_positions="none, fully paid for at entry",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
