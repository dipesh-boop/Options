"""Long strangle — new in Step 19A. A cheaper, wider-breakeven
volatility-expansion debit structure than the straddle: long an
out-of-the-money call and an out-of-the-money put."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy the out-of-the-money call and put in one combo order for a net debit.",)
DEFAULT_EXIT_RULES = ("Close once either leg's gain covers the total debit and the target profit fraction.",)
DEFAULT_ADJUSTMENT_RULES = ("Close the losing leg early only if volatility has clearly collapsed before expiration.",)
DEFAULT_INVALIDATION_RULES = ("Close if the volatility-expansion thesis is invalidated by a stated condition.",)


def build_long_strangle_position(
    *, call_strike: float, call_premium: float, put_strike: float, put_premium: float, contracts: int
) -> Position:
    if call_strike <= put_strike:
        raise ValueError("long_strangle requires call_strike above put_strike (else it is a straddle)")
    return Position(legs=[
        Leg(right=OptionRight.CALL, strike=call_strike, side=Side.BUY, entry_price=call_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=put_strike, side=Side.BUY, entry_price=put_premium, quantity=contracts),
    ])


def required_move_pct(*, spot: float, call_strike: float, put_strike: float, call_premium: float, put_premium: float) -> float:
    """The nearer of the two breakevens' distance from spot, as a
    fraction of spot -- same purpose as
    `src.strategies.long_straddle.required_move_pct`."""
    if spot <= 0:
        raise ValueError("spot must be positive")
    debit = call_premium + put_premium
    upper_move = (call_strike + debit) - spot
    lower_move = spot - (put_strike - debit)
    return min(abs(upper_move), abs(lower_move)) / spot


def evaluate_long_strangle(
    *,
    ticker: str,
    expiration: date,
    call_contract: OptionContract,
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
    position = build_long_strangle_position(
        call_strike=call_contract.strike, call_premium=call_contract.mid,
        put_strike=put_contract.strike, put_premium=put_contract.mid, contracts=num_contracts,
    )
    debit = call_contract.mid + put_contract.mid
    capital = debit * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.LONG_STRANGLE, ticker=ticker, expiration=expiration, position=position,
        contracts=[call_contract, put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="neutral", volatility_outlook="expansion",
        required_positions="none, fully paid for at entry",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
