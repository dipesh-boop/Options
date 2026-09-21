"""Short iron condor — new in Step 19A, evaluation-only (Tier 2, see
`src.strategies.base.StrategyKind`'s own docstring: 4 legs exceed
`TradeProposal`'s current 2-leg cap). A defined-risk, neutral,
volatility-contraction income structure: a put credit spread and a call
credit spread on the same underlying and expiration, sold together."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Sell both the put spread and the call spread together in one four-leg combo order for a net credit.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll the tested side away from the underlying, preserving the untested side's credit.",)
DEFAULT_INVALIDATION_RULES = ("Close if the range-bound thesis is invalidated by a stated condition.",)


def build_iron_condor_position(
    *, long_put_strike: float, long_put_premium: float, short_put_strike: float, short_put_premium: float,
    short_call_strike: float, short_call_premium: float, long_call_strike: float, long_call_premium: float,
    contracts: int,
) -> Position:
    if not (long_put_strike < short_put_strike < short_call_strike < long_call_strike):
        raise ValueError("iron_condor requires long_put < short_put < short_call < long_call strikes")
    return Position(legs=[
        Leg(right=OptionRight.PUT, strike=long_put_strike, side=Side.BUY, entry_price=long_put_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=short_put_strike, side=Side.SELL, entry_price=short_put_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=short_call_strike, side=Side.SELL, entry_price=short_call_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=long_call_strike, side=Side.BUY, entry_price=long_call_premium, quantity=contracts),
    ])


def evaluate_short_iron_condor(
    *,
    ticker: str,
    expiration: date,
    long_put_contract: OptionContract,
    short_put_contract: OptionContract,
    short_call_contract: OptionContract,
    long_call_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_iron_condor_position(
        long_put_strike=long_put_contract.strike, long_put_premium=long_put_contract.mid,
        short_put_strike=short_put_contract.strike, short_put_premium=short_put_contract.mid,
        short_call_strike=short_call_contract.strike, short_call_premium=short_call_contract.mid,
        long_call_strike=long_call_contract.strike, long_call_premium=long_call_contract.mid,
        contracts=num_contracts,
    )
    put_width = short_put_contract.strike - long_put_contract.strike
    call_width = long_call_contract.strike - short_call_contract.strike
    credit = (short_put_contract.mid - long_put_contract.mid) + (short_call_contract.mid - long_call_contract.mid)
    capital = max(max(put_width, call_width) - credit, 0.0) * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.SHORT_IRON_CONDOR, ticker=ticker, expiration=expiration, position=position,
        contracts=[long_put_contract, short_put_contract, short_call_contract, long_call_contract],
        limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="neutral", volatility_outlook="contraction",
        required_positions="none, defined-risk margin only (evaluation only -- see module docstring)",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
        fidelity_compatible=False,
        fidelity_incompatibility_reason=(
            "4-leg structure exceeds TradeProposal's current 2-leg cap; not yet wired to the Risk Engine or a "
            "FidelityTradeTicket -- see src.strategies.base.StrategyKind"
        ),
    )
