"""Long call butterfly — added in Step 19A as evaluation-only, wired to
a real order in Step 20A (`TradeProposal.legs` now caps at 4;
`StrategyType.LONG_CALL_BUTTERFLY` exists). A neutral, defined-risk,
low-cost structure: long the lower strike, short 2x the middle strike,
long the upper strike, all calls, equally spaced."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy 1x lower strike, sell 2x middle strike, buy 1x upper strike, equally spaced, one combo order.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target as the underlying approaches the middle strike.",)
DEFAULT_ADJUSTMENT_RULES = ("Generally not adjusted; the defined risk is small and pin risk near expiration is the main concern.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying moves decisively outside the profit zone well before expiration.",)


def _isclose(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def build_long_call_butterfly_position(
    *, lower_strike: float, lower_premium: float, middle_strike: float, middle_premium: float,
    upper_strike: float, upper_premium: float, contracts: int,
) -> Position:
    if not (lower_strike < middle_strike < upper_strike):
        raise ValueError("long_call_butterfly requires lower_strike < middle_strike < upper_strike")
    if not _isclose(middle_strike - lower_strike, upper_strike - middle_strike):
        raise ValueError("long_call_butterfly requires equally spaced wings")
    return Position(legs=[
        Leg(right=OptionRight.CALL, strike=lower_strike, side=Side.BUY, entry_price=lower_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=middle_strike, side=Side.SELL, entry_price=middle_premium, quantity=2 * contracts),
        Leg(right=OptionRight.CALL, strike=upper_strike, side=Side.BUY, entry_price=upper_premium, quantity=contracts),
    ])


def evaluate_long_call_butterfly(
    *,
    ticker: str,
    expiration: date,
    lower_contract: OptionContract,
    middle_contract: OptionContract,
    upper_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_long_call_butterfly_position(
        lower_strike=lower_contract.strike, lower_premium=lower_contract.mid,
        middle_strike=middle_contract.strike, middle_premium=middle_contract.mid,
        upper_strike=upper_contract.strike, upper_premium=upper_contract.mid, contracts=num_contracts,
    )
    debit = lower_contract.mid - 2 * middle_contract.mid + upper_contract.mid
    capital = max(debit, 0.0) * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.LONG_CALL_BUTTERFLY, ticker=ticker, expiration=expiration, position=position,
        contracts=[lower_contract, middle_contract, upper_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="neutral", volatility_outlook="contraction",
        required_positions="none, fully paid for at entry (evaluation only -- see module docstring)",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
        fidelity_compatible=True,
        fidelity_incompatibility_reason=None,
    )
