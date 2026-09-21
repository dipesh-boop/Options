"""Protective put (a.k.a. married put when stock and put are opened
together) — new in Step 19A. Exists primarily to REDUCE LOSS on an
existing (or simultaneously-established) share position, not to
generate standalone profit -- see module docstring pattern established
by `src.strategies.protective_collar` for the same hedge-evaluation
discipline."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Buy a put against 100+ already-held (or simultaneously-purchased) shares.",)
DEFAULT_EXIT_RULES = ("Let the put expire worthless if unneeded.", "Exercise or sell the put if the hedge is needed.")
DEFAULT_ADJUSTMENT_RULES = ("Roll the put down and out to extend protection at a lower cost basis.",)
DEFAULT_INVALIDATION_RULES = ("Remove the hedge if the protection thesis (downside risk) is invalidated.",)


def build_protective_put_position(*, strike: float, premium: float, contracts: int, cost_basis: float) -> Position:
    return Position(
        legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.BUY, entry_price=premium, quantity=contracts)],
        underlying_shares=100 * contracts, underlying_cost_basis=cost_basis,
    )


def evaluate_protective_put(
    *,
    ticker: str,
    expiration: date,
    put_contract: OptionContract,
    cost_basis: float,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    limits: RiskLimitsConfig,
    event_risk: RiskLevel = "low",
) -> StrategyEvaluation:
    position = build_protective_put_position(
        strike=put_contract.strike, premium=put_contract.mid, contracts=num_contracts, cost_basis=cost_basis
    )
    capital = cost_basis * 100 * num_contracts
    buying_power = put_contract.mid * 100 * num_contracts  # only the premium is new cash out; shares already owned
    return build_strategy_evaluation(
        kind=StrategyKind.PROTECTIVE_PUT, ticker=ticker, expiration=expiration, position=position,
        contracts=[put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="protection", volatility_outlook="irrelevant",
        required_positions=f"{100 * num_contracts} shares of {ticker} already held",
        capital_requirement=capital, buying_power_requirement=buying_power,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
