"""Covered call — retained unchanged from the platform's original three
strategies. Requires 100 shares per contract already held."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Sell a call against 100+ already-held shares at or above the target delta.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll up and out if the shares approach the strike before expiration.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_covered_call_position(*, strike: float, premium: float, contracts: int, cost_basis: float) -> Position:
    return Position(
        legs=[Leg(right=OptionRight.CALL, strike=strike, side=Side.SELL, entry_price=premium, quantity=contracts)],
        underlying_shares=100 * contracts, underlying_cost_basis=cost_basis,
    )


def evaluate_covered_call(
    *,
    ticker: str,
    expiration: date,
    call_contract: OptionContract,
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
    """Ex-dividend risk (assignment before an ex-div date, forfeiting
    the dividend) is folded into `assignment_risk` via
    `src.strategies.base._assignment_and_exercise_risk`'s ITM check —
    genuinely separate ex-dividend-calendar data is a documented gap
    (no dividend calendar exists anywhere in this codebase yet, the
    same gap `SECURITY_AUDIT.md`'s OP-005 already names)."""
    position = build_covered_call_position(
        strike=call_contract.strike, premium=call_contract.mid, contracts=num_contracts, cost_basis=cost_basis
    )
    capital = cost_basis * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.COVERED_CALL, ticker=ticker, expiration=expiration, position=position,
        contracts=[call_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="neutral", volatility_outlook="neutral",
        required_positions=f"{100 * num_contracts} shares of {ticker} already held",
        capital_requirement=capital, buying_power_requirement=0.0,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
