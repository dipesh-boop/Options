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

import math
from dataclasses import dataclass

from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position, monte_carlo_pop_and_ev, payoff_profile
from src.quant.probability import probability_above, probability_below, probability_of_profit

# Fixed seed/path-count for the Monte Carlo expected-value cross-check
# used by the unbounded-upside strategies below (long call, long
# straddle, long strangle) -- deterministic and reproducible, the same
# reasoning MD-001's fix gave for never letting src/quant's output
# depend on wall-clock/random state a caller didn't explicitly supply.
_MC_PATHS = 20_000
_MC_SEED = 20260101


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
    # Step 19A addition: additive, defaults to None so every existing
    # caller/construction of a one-breakeven strategy (CSP, covered
    # call, put/call credit spread, bull/bear spread, long call/put,
    # protective put/collar) is unaffected. Set only for the two-sided
    # strategies (long straddle, long strangle), where `breakeven` above
    # is defined as the LOWER of the two.
    breakeven_upper: float | None = None


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


# ---------------------------------------- Step 19A: expanded strategy library --
#
# Same "explicit numeric parameters, trivially hand-checkable against
# the textbook formula" discipline as the three functions above. Every
# formula here is a standard, widely-published options-strategy
# identity (e.g. bull_call_spread max profit = width - debit) --
# reused, not derived ad hoc.


def call_credit_spread_max_profit(credit: float, contracts: int = 1) -> float:
    return credit * 100 * contracts


def call_credit_spread_max_loss(short_strike: float, long_strike: float, credit: float, contracts: int = 1) -> float:
    width = long_strike - short_strike
    if width <= 0:
        raise ValueError("long_strike must be above short_strike for a call credit spread")
    return max(width - credit, 0.0) * 100 * contracts


def call_credit_spread_breakeven(short_strike: float, credit: float) -> float:
    return short_strike + credit


def call_credit_spread_economics(
    spot: float, short_strike: float, long_strike: float, credit: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    max_profit = call_credit_spread_max_profit(credit, contracts)
    max_loss = call_credit_spread_max_loss(short_strike, long_strike, credit, contracts)
    breakeven = call_credit_spread_breakeven(short_strike, credit)
    capital_required = max_loss
    pop = probability_below(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry), probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


def bull_call_spread_max_profit(long_strike: float, short_strike: float, debit: float, contracts: int = 1) -> float:
    width = short_strike - long_strike
    if width <= 0:
        raise ValueError("short_strike must be above long_strike for a bull call spread")
    return max(width - debit, 0.0) * 100 * contracts


def bull_call_spread_max_loss(debit: float, contracts: int = 1) -> float:
    return max(debit, 0.0) * 100 * contracts


def bull_call_spread_breakeven(long_strike: float, debit: float) -> float:
    return long_strike + debit


def bull_call_spread_economics(
    spot: float, long_strike: float, short_strike: float, debit: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    max_profit = bull_call_spread_max_profit(long_strike, short_strike, debit, contracts)
    max_loss = bull_call_spread_max_loss(debit, contracts)
    breakeven = bull_call_spread_breakeven(long_strike, debit)
    capital_required = max_loss  # fully paid for at entry -- the debit is the only capital consumed
    pop = probability_above(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry), probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


def bear_put_spread_max_profit(long_strike: float, short_strike: float, debit: float, contracts: int = 1) -> float:
    width = long_strike - short_strike
    if width <= 0:
        raise ValueError("long_strike must be above short_strike for a bear put spread")
    return max(width - debit, 0.0) * 100 * contracts


def bear_put_spread_max_loss(debit: float, contracts: int = 1) -> float:
    return max(debit, 0.0) * 100 * contracts


def bear_put_spread_breakeven(long_strike: float, debit: float) -> float:
    return long_strike - debit


def bear_put_spread_economics(
    spot: float, long_strike: float, short_strike: float, debit: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    max_profit = bear_put_spread_max_profit(long_strike, short_strike, debit, contracts)
    max_loss = bear_put_spread_max_loss(debit, contracts)
    breakeven = bear_put_spread_breakeven(long_strike, debit)
    capital_required = max_loss
    pop = probability_below(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry), probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


def protective_put_max_loss(cost_basis: float, put_strike: float, premium: float, contracts: int = 1) -> float:
    """The floor P&L per share below the strike is constant
    (put_strike - cost_basis - premium); reported as a loss (>=0), same
    `max(..., 0.0)` convention as `covered_call_max_loss` for the case
    where that floor is actually a guaranteed gain (e.g. a deep-ITM
    protective put bought below cost basis)."""
    return max(cost_basis - put_strike + premium, 0.0) * 100 * contracts


def protective_put_breakeven(cost_basis: float, premium: float) -> float:
    return cost_basis + premium


def protective_put_economics(
    spot: float, put_strike: float, premium: float, cost_basis: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    """Max profit is unbounded (the long-share leg has no cap) --
    reported as `math.inf`, the one field `src.risk.trade_risk
    .QuantitativeAnalysis` explicitly allows to be non-finite (see that
    module's docstring). `expected_value` is instead sourced from the
    Monte Carlo cross-check (`_MC_PATHS`/`_MC_SEED`), since the binary
    max-profit/max-loss heuristic used by every bounded strategy above
    is meaningless once max_profit is infinite."""
    max_loss = protective_put_max_loss(cost_basis, put_strike, premium, contracts)
    breakeven = protective_put_breakeven(cost_basis, premium)
    capital_required = cost_basis * 100 * contracts
    pop = probability_above(spot, breakeven, t, rate, sigma)
    position = Position(
        legs=[Leg(right=OptionRight.PUT, strike=put_strike, side=Side.BUY, entry_price=premium, quantity=contracts)],
        underlying_shares=100 * contracts, underlying_cost_basis=cost_basis,
    )
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else mc.expected_value / capital_required
    return StrategyEconomics(
        max_profit=math.inf, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=pop, expected_value=mc.expected_value,
    )


def protective_collar_max_profit(call_strike: float, cost_basis: float, net_credit: float, contracts: int = 1) -> float:
    return (call_strike - cost_basis + net_credit) * 100 * contracts


def protective_collar_max_loss(cost_basis: float, put_strike: float, net_credit: float, contracts: int = 1) -> float:
    return max(cost_basis - put_strike - net_credit, 0.0) * 100 * contracts


def protective_collar_breakeven(cost_basis: float, net_credit: float) -> float:
    return cost_basis - net_credit


def protective_collar_economics(
    spot: float, call_strike: float, put_strike: float, net_credit: float, cost_basis: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    """`net_credit` = call premium received minus put premium paid
    (positive = net credit collar, negative = net debit collar; both
    are valid and common). Both profit and loss are capped -- a collar
    is a defined-risk, defined-reward structure by construction."""
    max_profit = protective_collar_max_profit(call_strike, cost_basis, net_credit, contracts)
    max_loss = protective_collar_max_loss(cost_basis, put_strike, net_credit, contracts)
    breakeven = protective_collar_breakeven(cost_basis, net_credit)
    capital_required = cost_basis * 100 * contracts
    pop = probability_above(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry), probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


def long_call_max_loss(premium: float, contracts: int = 1) -> float:
    return max(premium, 0.0) * 100 * contracts


def long_call_breakeven(strike: float, premium: float) -> float:
    return strike + premium


def long_call_economics(
    spot: float, strike: float, premium: float, t: float, rate: float, sigma: float,
    days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    max_loss = long_call_max_loss(premium, contracts)
    breakeven = long_call_breakeven(strike, premium)
    capital_required = max_loss
    pop = probability_above(spot, breakeven, t, rate, sigma)
    position = Position(legs=[Leg(right=OptionRight.CALL, strike=strike, side=Side.BUY, entry_price=premium, quantity=contracts)])
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else mc.expected_value / capital_required
    return StrategyEconomics(
        max_profit=math.inf, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=pop, expected_value=mc.expected_value,
    )


def long_put_max_profit(strike: float, premium: float, contracts: int = 1) -> float:
    return max(strike - premium, 0.0) * 100 * contracts


def long_put_max_loss(premium: float, contracts: int = 1) -> float:
    return max(premium, 0.0) * 100 * contracts


def long_put_breakeven(strike: float, premium: float) -> float:
    return strike - premium


def long_put_economics(
    spot: float, strike: float, premium: float, t: float, rate: float, sigma: float,
    days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    max_profit = long_put_max_profit(strike, premium, contracts)
    max_loss = long_put_max_loss(premium, contracts)
    breakeven = long_put_breakeven(strike, premium)
    capital_required = max_loss
    pop = probability_below(spot, breakeven, t, rate, sigma)
    roc = _return_on_capital(max_profit, capital_required)
    return StrategyEconomics(
        max_profit=max_profit, max_loss=max_loss, breakeven=breakeven, capital_required=capital_required,
        return_on_capital=roc, annualized_roc=_annualize(roc, days_to_expiry), probability_of_profit=pop,
        expected_value=_binary_expected_value(pop, max_profit, max_loss),
    )


def _straddle_strangle_max_loss(debit: float, contracts: int = 1) -> float:
    return max(debit, 0.0) * 100 * contracts


def long_straddle_economics(
    spot: float, strike: float, call_premium: float, put_premium: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    debit = call_premium + put_premium
    max_loss = _straddle_strangle_max_loss(debit, contracts)
    breakeven_lower = strike - debit
    breakeven_upper = strike + debit
    capital_required = max_loss
    pop = probability_below(spot, breakeven_lower, t, rate, sigma) + probability_above(spot, breakeven_upper, t, rate, sigma)
    position = Position(legs=[
        Leg(right=OptionRight.CALL, strike=strike, side=Side.BUY, entry_price=call_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=strike, side=Side.BUY, entry_price=put_premium, quantity=contracts),
    ])
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else mc.expected_value / capital_required
    return StrategyEconomics(
        max_profit=math.inf, max_loss=max_loss, breakeven=breakeven_lower, breakeven_upper=breakeven_upper,
        capital_required=capital_required, return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=pop, expected_value=mc.expected_value,
    )


# ---------------------------------------- Step 20A: 3/4-leg structures --
#
# Unlike every strategy above (whose profit zone is bounded by a single
# breakeven, on one side or between two symmetric tails), a long call
# butterfly / short iron condor / short iron butterfly's profit zone
# sits *between* its two breakevens, with a payoff shape that has more
# than one kink -- the "probability of profit above/below one
# breakeven" closed forms above don't apply. Rather than hand-derive a
# new probability formula for each of these 3 shapes, these functions
# use the generic, exact `payoff_profile` engine (max profit/max loss/
# breakevens, from the same piecewise-linear analysis every
# `src.strategies.*` module already relies on) plus the full simulated
# Monte Carlo payoff distribution for probability_of_profit/expected_value
# (which integrates the real payoff curve regardless of its shape) --
# the same "generic payoff engine reduces dependence on hand-written
# formulas" approach Step 20A calls for, and the same pattern
# `protective_put_economics`/`long_call_economics` already use for an
# unbounded-upside shape above.


def long_call_butterfly_economics(
    spot: float, lower_strike: float, middle_strike: float, upper_strike: float,
    lower_premium: float, middle_premium: float, upper_premium: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    """Buy 1x lower call, sell 2x middle call, buy 1x upper call, all
    same expiration, equally spaced. Defined risk (the net debit) and
    defined reward (wing width minus debit) on both sides."""
    if not (lower_strike < middle_strike < upper_strike):
        raise ValueError("long_call_butterfly requires lower_strike < middle_strike < upper_strike")
    position = Position(legs=[
        Leg(right=OptionRight.CALL, strike=lower_strike, side=Side.BUY, entry_price=lower_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=middle_strike, side=Side.SELL, entry_price=middle_premium, quantity=2 * contracts),
        Leg(right=OptionRight.CALL, strike=upper_strike, side=Side.BUY, entry_price=upper_premium, quantity=contracts),
    ])
    profile = payoff_profile(position)
    debit = lower_premium - 2 * middle_premium + upper_premium
    capital_required = max(debit, 0.0) * 100 * contracts
    breakevens = profile.breakeven_points
    breakeven = breakevens[0] if breakevens else lower_strike
    breakeven_upper = breakevens[1] if len(breakevens) > 1 else None
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else _return_on_capital(profile.max_profit, capital_required)
    return StrategyEconomics(
        max_profit=profile.max_profit, max_loss=profile.max_loss, breakeven=breakeven, breakeven_upper=breakeven_upper,
        capital_required=capital_required, return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=mc.probability_of_profit, expected_value=mc.expected_value,
    )


def short_iron_condor_economics(
    spot: float, long_put_strike: float, short_put_strike: float, short_call_strike: float, long_call_strike: float,
    long_put_premium: float, short_put_premium: float, short_call_premium: float, long_call_premium: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    """Buy lower put, sell higher put, sell lower call, sell higher... —
    buy long_put < sell short_put < sell short_call < buy long_call,
    same expiration, 1:-1:-1:1. Defined-risk margin is the wider of the
    two wing widths minus the net credit received (only one side can
    ever be in-the-money at expiration, so the wider wing never adds to
    the narrower one's own worst case)."""
    if not (long_put_strike < short_put_strike < short_call_strike < long_call_strike):
        raise ValueError("short_iron_condor requires long_put < short_put < short_call < long_call strikes")
    position = Position(legs=[
        Leg(right=OptionRight.PUT, strike=long_put_strike, side=Side.BUY, entry_price=long_put_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=short_put_strike, side=Side.SELL, entry_price=short_put_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=short_call_strike, side=Side.SELL, entry_price=short_call_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=long_call_strike, side=Side.BUY, entry_price=long_call_premium, quantity=contracts),
    ])
    profile = payoff_profile(position)
    put_width = short_put_strike - long_put_strike
    call_width = long_call_strike - short_call_strike
    credit = (short_put_premium - long_put_premium) + (short_call_premium - long_call_premium)
    capital_required = max(max(put_width, call_width) - credit, 0.0) * 100 * contracts
    breakevens = profile.breakeven_points
    breakeven = breakevens[0] if breakevens else short_put_strike
    breakeven_upper = breakevens[1] if len(breakevens) > 1 else None
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else _return_on_capital(profile.max_profit, capital_required)
    return StrategyEconomics(
        max_profit=profile.max_profit, max_loss=profile.max_loss, breakeven=breakeven, breakeven_upper=breakeven_upper,
        capital_required=capital_required, return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=mc.probability_of_profit, expected_value=mc.expected_value,
    )


def short_iron_butterfly_economics(
    spot: float, put_wing_strike: float, center_strike: float, call_wing_strike: float,
    put_wing_premium: float, center_put_premium: float, center_call_premium: float, call_wing_premium: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    """Buy put wing, sell center put + sell center call (same strike),
    buy call wing, same expiration, 1:-1:-1:1. Explicitly the SHORT
    variant -- a net credit collected at entry, never a net debit."""
    if not (put_wing_strike < center_strike < call_wing_strike):
        raise ValueError("short_iron_butterfly requires put_wing_strike < center_strike < call_wing_strike")
    position = Position(legs=[
        Leg(right=OptionRight.PUT, strike=put_wing_strike, side=Side.BUY, entry_price=put_wing_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=center_strike, side=Side.SELL, entry_price=center_put_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=center_strike, side=Side.SELL, entry_price=center_call_premium, quantity=contracts),
        Leg(right=OptionRight.CALL, strike=call_wing_strike, side=Side.BUY, entry_price=call_wing_premium, quantity=contracts),
    ])
    profile = payoff_profile(position)
    put_width = center_strike - put_wing_strike
    call_width = call_wing_strike - center_strike
    credit = (center_put_premium - put_wing_premium) + (center_call_premium - call_wing_premium)
    capital_required = max(max(put_width, call_width) - credit, 0.0) * 100 * contracts
    breakevens = profile.breakeven_points
    breakeven = breakevens[0] if breakevens else center_strike
    breakeven_upper = breakevens[1] if len(breakevens) > 1 else None
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else _return_on_capital(profile.max_profit, capital_required)
    return StrategyEconomics(
        max_profit=profile.max_profit, max_loss=profile.max_loss, breakeven=breakeven, breakeven_upper=breakeven_upper,
        capital_required=capital_required, return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=mc.probability_of_profit, expected_value=mc.expected_value,
    )


def long_strangle_economics(
    spot: float, call_strike: float, put_strike: float, call_premium: float, put_premium: float,
    t: float, rate: float, sigma: float, days_to_expiry: int, contracts: int = 1,
) -> StrategyEconomics:
    if call_strike <= put_strike:
        raise ValueError("call_strike must be above put_strike for a strangle")
    debit = call_premium + put_premium
    max_loss = _straddle_strangle_max_loss(debit, contracts)
    breakeven_lower = put_strike - debit
    breakeven_upper = call_strike + debit
    capital_required = max_loss
    pop = probability_below(spot, breakeven_lower, t, rate, sigma) + probability_above(spot, breakeven_upper, t, rate, sigma)
    position = Position(legs=[
        Leg(right=OptionRight.CALL, strike=call_strike, side=Side.BUY, entry_price=call_premium, quantity=contracts),
        Leg(right=OptionRight.PUT, strike=put_strike, side=Side.BUY, entry_price=put_premium, quantity=contracts),
    ])
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    roc = 0.0 if capital_required <= 0 else mc.expected_value / capital_required
    return StrategyEconomics(
        max_profit=math.inf, max_loss=max_loss, breakeven=breakeven_lower, breakeven_upper=breakeven_upper,
        capital_required=capital_required, return_on_capital=roc,
        annualized_roc=_annualize(roc, days_to_expiry) if days_to_expiry > 0 else roc,
        probability_of_profit=pop, expected_value=mc.expected_value,
    )
