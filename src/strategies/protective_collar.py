"""Protective collar — new in Step 19A. Exists primarily to REDUCE LOSS
on an existing share position, funded (partially or fully) by selling a
call against the same shares — a defined-risk, defined-reward hedge."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Sell a call and buy a put against 100+ already-held shares in one combo order.",)
DEFAULT_EXIT_RULES = ("Let both legs expire if the underlying stays inside the collar's strikes.",)
DEFAULT_ADJUSTMENT_RULES = ("Roll the call up/out if the shares approach the ceiling before expiration.",)
DEFAULT_INVALIDATION_RULES = ("Remove the hedge if the protection thesis is invalidated.",)


def build_protective_collar_position(
    *, call_strike: float, call_premium: float, put_strike: float, put_premium: float, contracts: int, cost_basis: float
) -> Position:
    return Position(
        legs=[
            Leg(right=OptionRight.CALL, strike=call_strike, side=Side.SELL, entry_price=call_premium, quantity=contracts),
            Leg(right=OptionRight.PUT, strike=put_strike, side=Side.BUY, entry_price=put_premium, quantity=contracts),
        ],
        underlying_shares=100 * contracts, underlying_cost_basis=cost_basis,
    )


def evaluate_protective_collar(
    *,
    ticker: str,
    expiration: date,
    call_contract: OptionContract,
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
    position = build_protective_collar_position(
        call_strike=call_contract.strike, call_premium=call_contract.mid,
        put_strike=put_contract.strike, put_premium=put_contract.mid,
        contracts=num_contracts, cost_basis=cost_basis,
    )
    capital = cost_basis * 100 * num_contracts
    net_credit = call_contract.mid - put_contract.mid
    buying_power = max(-net_credit, 0.0) * 100 * num_contracts  # only a net-debit collar consumes new cash
    return build_strategy_evaluation(
        kind=StrategyKind.PROTECTIVE_COLLAR, ticker=ticker, expiration=expiration, position=position,
        contracts=[call_contract, put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="protection", volatility_outlook="irrelevant",
        required_positions=f"{100 * num_contracts} shares of {ticker} already held",
        capital_requirement=capital, buying_power_requirement=buying_power,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
