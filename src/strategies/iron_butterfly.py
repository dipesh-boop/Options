"""Short iron butterfly — added in Step 19A as evaluation-only, wired to
a real order in Step 20A (`TradeProposal.legs` now caps at 4;
`StrategyType.SHORT_IRON_BUTTERFLY` exists). A defined-risk, neutral,
volatility-contraction income structure: a short straddle at the center
strike, wrapped with a long put and a long call at the wings for
defined risk. Explicitly the SHORT variant -- this module never
constructs a long iron butterfly, which would be a materially different
(net-debit, direction-agnostic-loss) structure."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = (
    "Sell the at-the-money call and put at the center strike, buy the put and call wings, one four-leg combo order for a net credit.",
)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Generally not adjusted once tested; the defined risk is already at its maximum on either wing.",)
DEFAULT_INVALIDATION_RULES = ("Close if the pinning/range-bound thesis is invalidated by a stated condition.",)


def build_short_iron_butterfly_position(
    *, put_wing_strike: float, put_wing_premium: float, center_strike: float,
    center_call_premium: float, center_put_premium: float, call_wing_strike: float, call_wing_premium: float,
    contracts: int,
) -> Position:
    if not (put_wing_strike < center_strike < call_wing_strike):
        raise ValueError("short_iron_butterfly requires put_wing_strike < center_strike < call_wing_strike")
    return Position(legs=[
        Leg(right=OptionRight.PUT, strike=put_wing_strike, side=Side.BUY, entry_price=put_wing_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=center_strike, side=Side.SELL, entry_price=center_put_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=center_strike, side=Side.SELL, entry_price=center_call_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=call_wing_strike, side=Side.BUY, entry_price=call_wing_premium, quantity=contracts),
    ])


def evaluate_short_iron_butterfly(
    *,
    ticker: str,
    expiration: date,
    put_wing_contract: OptionContract,
    center_put_contract: OptionContract,
    center_call_contract: OptionContract,
    call_wing_contract: OptionContract,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    if center_put_contract.strike != center_call_contract.strike:
        raise ValueError("short_iron_butterfly requires the center put and call to share the same strike")
    position = build_short_iron_butterfly_position(
        put_wing_strike=put_wing_contract.strike, put_wing_premium=put_wing_contract.mid,
        center_strike=center_put_contract.strike,
        center_call_premium=center_call_contract.mid, center_put_premium=center_put_contract.mid,
        call_wing_strike=call_wing_contract.strike, call_wing_premium=call_wing_contract.mid,
        contracts=num_contracts,
    )
    put_width = center_put_contract.strike - put_wing_contract.strike
    call_width = call_wing_contract.strike - center_call_contract.strike
    credit = (center_put_contract.mid - put_wing_contract.mid) + (center_call_contract.mid - call_wing_contract.mid)
    capital = max(max(put_width, call_width) - credit, 0.0) * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.SHORT_IRON_BUTTERFLY, ticker=ticker, expiration=expiration, position=position,
        contracts=[put_wing_contract, center_put_contract, center_call_contract, call_wing_contract],
        limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="neutral", volatility_outlook="contraction",
        required_positions="none, defined-risk margin only (evaluation only -- see module docstring)",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
        fidelity_compatible=True,
        fidelity_incompatibility_reason=None,
    )
