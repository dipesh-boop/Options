"""Per-strategy payoff structure (max profit, max loss, breakeven),
return on capital, and expected value — the platform's three initial
strategies only: cash-secured put, covered call, put credit spread.

Expected value here uses a simplified two-outcome model (probability of
profit x max profit, weighted against probability of loss x max loss).
This is a standard options-education heuristic, not exact — the true
payoff is continuous between breakeven and the max-loss point, not
binary. src.quant.monte_carlo provides a Monte Carlo estimate that
integrates the full simulated terminal-price distribution as a more
rigorous cross-check (see ARCHITECTURE.md §6).

Each strategy exposes plain-float building-block functions
(`*_max_profit`, `*_max_loss`, `*_breakeven`) alongside a `*_economics`
convenience that also folds in probability of profit and expected
value. The building blocks take explicit numeric parameters (strike,
credit, cost basis, width) rather than a shared position object, on
purpose — it keeps them trivially hand-checkable against the textbook
formula, which is exactly what "independently verifiable mathematical
cases" in the test suite relies on.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.quant.probability import probability_of_profit


@dataclass(frozen=True)
class StrategyEconomics:
    max_profit: float
    max_loss: float
    breakeven: float
    capital_required: float
    return_on_capital: float
    annualized_roc: float
    probability_of_profit: float
    expected_value: float


def _return_on_capital(max_profit: float, capital_required: float) -> float:
    if capital_required <= 0:
        raise ValueError("capital_required must be positive")
    return max_profit / capital_required


def _annualize(roc: float, days_to_expiry: int) -> float:
    if days_to_expiry <= 0:
        raise ValueError("days_to_expiry must be positive")
    return roc * (365.0 / days_to_expiry)


def _binary_expected_value(pop: float, max_profit: float, max_loss: float) -> float:
    return pop * max_profit - (1.0 - pop) * max_loss


# ---------------------------------------------------------------- CSP --

def csp_max_profit(credit: float, contracts: int = 1) -> float:
    return credit * 100 * contracts


def csp_max_loss(strike: float, credit: float, contracts: int = 1) -> float:
    return max(strike - credit, 0.0) * 100 * contracts


def csp_breakeven(strike: float, credit: float) -> float:
    return strike - credit


def csp_economics(
    spot: float,
    strike: float,
    credit: float,
    t: float,
    rate: float,
    sigma: float,
    days_to_expiry: int,
    contracts: int = 1,
) -> StrategyEconomics:
    max_profit = csp_max_profit(credit, contracts)
    max_loss = csp_max_loss(strike, credit, contracts)
    breakeven = csp_breakeven(strike, credit)
    capital_required = strike * 100 * contracts
    pop = probability_of_profit(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        capital_required=capital_required,
        return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry),
        probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


# ------------------------------------------------------- Covered call --

def covered_call_max_profit(strike: float, credit: float, cost_basis: float, contracts: int = 1) -> float:
    return (strike - cost_basis + credit) * 100 * contracts


def covered_call_max_loss(cost_basis: float, credit: float, contracts: int = 1) -> float:
    return max(cost_basis - credit, 0.0) * 100 * contracts


def covered_call_breakeven(cost_basis: float, credit: float) -> float:
    return cost_basis - credit


def covered_call_economics(
    spot: float,
    strike: float,
    credit: float,
    cost_basis: float,
    t: float,
    rate: float,
    sigma: float,
    days_to_expiry: int,
    contracts: int = 1,
) -> StrategyEconomics:
    max_profit = covered_call_max_profit(strike, credit, cost_basis, contracts)
    max_loss = covered_call_max_loss(cost_basis, credit, contracts)
    breakeven = covered_call_breakeven(cost_basis, credit)
    capital_required = cost_basis * 100 * contracts
    pop = probability_of_profit(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        capital_required=capital_required,
        return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry),
        probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


# --------------------------------------------------- Put credit spread --

def put_credit_spread_max_profit(credit: float, contracts: int = 1) -> float:
    return credit * 100 * contracts


def put_credit_spread_max_loss(short_strike: float, long_strike: float, credit: float, contracts: int = 1) -> float:
    width = short_strike - long_strike
    if width <= 0:
        raise ValueError("short_strike must be above long_strike for a put credit spread")
    return max(width - credit, 0.0) * 100 * contracts


def put_credit_spread_breakeven(short_strike: float, credit: float) -> float:
    return short_strike - credit


def put_credit_spread_economics(
    spot: float,
    short_strike: float,
    long_strike: float,
    credit: float,
    t: float,
    rate: float,
    sigma: float,
    days_to_expiry: int,
    contracts: int = 1,
) -> StrategyEconomics:
    max_profit = put_credit_spread_max_profit(credit, contracts)
    max_loss = put_credit_spread_max_loss(short_strike, long_strike, credit, contracts)
    breakeven = put_credit_spread_breakeven(short_strike, credit)
    capital_required = max_loss  # standard defined-risk margin for a credit spread
    pop = probability_of_profit(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        capital_required=capital_required,
        return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry),
        probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )
