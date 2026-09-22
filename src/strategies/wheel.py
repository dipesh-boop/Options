"""Wheel candidate evaluation for strategy competition (Part 16): "Is
initiating this Wheel better on a risk-adjusted basis than the
alternatives, including holding cash?"

A fresh Wheel's only *known, priced* leg at candidate time is its entry
CSP — whether it is ever assigned, and what covered calls follow if it
is, are genuinely unknown at entry (CLAUDE.md: never fabricate a price,
Greek, or probability). So this module's `StrategyEvaluation` for
`StrategyKind.WHEEL` reuses the exact same CSP economics
`src.strategies.cash_secured_put` already computes — same position, same
Monte Carlo, same Quant — and does not invent a second, speculative
multi-cycle projection to make the Wheel look more attractive than a
standalone CSP. The only real difference the comparison layer
(`src.strategies.selector`/`comparison`) should draw between a Wheel
candidate and a plain CASH_SECURED_PUT candidate on the same contract is
qualitative: a Wheel is entered with the explicit intent to keep and
work the underlying if assigned (Part 4's "only on securities the system
would be comfortable owning"), not to avoid assignment."""
from __future__ import annotations

from datetime import date

from src.data.option_chain import OptionContract
from src.risk.limits import RiskLimitsConfig
from src.strategies.base import RiskLevel, StrategyEvaluation, StrategyKind, build_strategy_evaluation
from src.strategies.cash_secured_put import build_cash_secured_put_position

DEFAULT_ENTRY_RULES = (
    "Sell a cash-secured put only on an underlying the system would be comfortable owning (see "
    "src.wheel.eligibility.check_wheel_eligibility) at or below the target delta.",
)
DEFAULT_EXIT_RULES = (
    "If unassigned: close at the configured profit target or let expire worthless, then re-evaluate "
    "as a brand-new candidate -- never automatically reopen another put on the same wheel_id.",
    "If assigned: begin selling covered calls against the shares (src.wheel.lifecycle) until called "
    "away or manually exited.",
)
DEFAULT_ADJUSTMENT_RULES = (
    "Any roll closes the existing option, realizes its P&L, and re-evaluates a new option as an "
    "independent proposal through the full pipeline -- never a blind same-day same-strike-different-date roll.",
)
DEFAULT_INVALIDATION_RULES = (
    "Would we still want the stock after a 20% decline? If the answer becomes no, this is not a Wheel "
    "candidate regardless of how attractive the premium looks.",
)


def evaluate_wheel_candidate(
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
        kind=StrategyKind.WHEEL, ticker=ticker, expiration=expiration, position=position,
        contracts=[put_contract], limits=limits, spot=spot, sigma=sigma, t=t, rate=rate,
        days_to_expiry=days_to_expiry, num_contracts=num_contracts,
        market_outlook="moderately_bullish", volatility_outlook="neutral",
        required_positions="cash collateral only at entry; may become 100+ shares per contract if assigned",
        capital_requirement=capital, buying_power_requirement=capital,
        entry_rules=DEFAULT_ENTRY_RULES, exit_rules=DEFAULT_EXIT_RULES,
        adjustment_rules=DEFAULT_ADJUSTMENT_RULES, invalidation_rules=DEFAULT_INVALIDATION_RULES,
        event_risk=event_risk,
    )
