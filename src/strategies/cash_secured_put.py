"""Cash-secured put — retained unchanged from the platform's original
three strategies (Step 9). This module is the `src/strategies/` wrapper
around the exact same math `src.risk.trade_risk`/`src.quant
.expected_value.csp_economics` already use for a real order; nothing
here recomputes anything differently."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation

DEFAULT_ENTRY_RULES = ("Sell a put at or below the target delta on a liquid, in-universe underlying.",)
DEFAULT_EXIT_RULES = ("Close at the configured profit target.", "Manage at the configured management DTE.")
DEFAULT_ADJUSTMENT_RULES = ("Roll down and out if tested, preserving credit received.",)
DEFAULT_INVALIDATION_RULES = ("Close if the underlying thesis is invalidated by a stated condition.",)


def build_cash_secured_put_position(*, strike: float, premium: float, contracts: int) -> Position:
    return Position(legs=[Leg(right=OptionRight.PUT, strike=strike, side=Side.SELL, entry_price=premium, quantity=contracts)])


def evaluate_cash_secured_put(
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
    position = build_cash_secured_put_position(strike=put_contract.strike, premium=put_contract.mid, contracts=num_contracts)
    capital = put_contract.strike * 100 * num_contracts
    return build_strategy_evaluation(
        kind=StrategyKind.CASH_SECURED_PUT, ticker=ticker, expiration=expiration, position=position,
        contracts=[put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="moderately_bullish", volatility_outlook="neutral",
        required_positions="cash collateral only, no existing shares required",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
