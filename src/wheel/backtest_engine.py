"""Stateful Wheel backtest engine (Part 15).

`src.backtest.engine.run_backtest` treats every position as an
independent round trip — correct for the platform's other 16 strategies,
none of which carry state across positions. A Wheel's CC leg is not
independent of its CSP leg: the CC's collateral IS the CSP's own
assignment, and the Wheel's basis/premium accounting must persist across
every cycle for the whole run. This module drives `src.wheel.lifecycle`
through exactly the same state machine the live path
(`src.wheel.paper_events`) uses, fed by historical quotes instead of live
`PaperBroker` fills — reusing `src.backtest.execution` (fill simulation)
and `src.backtest.assignment` (expiration settlement) for the actual
pricing math, never a second, backtest-specific pricing model.

**No look-ahead.** Every quote lookup here is piped through
`assert_no_lookahead_options(quote_lookup(...), as_of)` before use,
exactly like `src.backtest.engine.run_backtest` — a day-N decision can no
more see a day-N+k quote here than it can there.

**Never manufactures a missing option chain.** If no entry candidate
exists on a given day (Part 4/5's own eligibility rules aren't
re-implemented here — this is about *statefulness*, not re-running the
live Risk/eligibility pipeline against history), the engine simply does
not enter that day; if a position that must be priced (to close it, or
because it's expiring) has no matching quote at all,
`WheelBacktestDataInsufficientError` is raised rather than guessing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from src.backtest.assignment import realized_settlement_pnl, settle_position
from src.backtest.commissions import CommissionSchedule, calculate_commission
from src.backtest.execution import execute_entry
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote, assert_no_lookahead_options
from src.brokers.paper import PaperBrokerConfig
from src.data.option_chain import OptionRight
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.quant.greeks import delta as bs_delta
from src.wheel import lifecycle
from src.wheel.models import WheelPosition
from src.wheel.state import WheelState

_CONTRACT_MULTIPLIER = 100
_TZ_NOON_UTC_OFFSET = "T12:00:00+00:00"  # backtest dates -> a fixed, arbitrary tz-aware instant WheelPosition requires


class WheelBacktestDataInsufficientError(ValueError):
    """Part 15: "Where historical option data is insufficient:
    BACKTEST_DATA_INSUFFICIENT. Do not manufacture missing option
    chains." Raised instead of silently skipping a settlement or
    inventing a settlement price."""


def _as_datetime(d: date):
    from datetime import datetime

    return datetime.fromisoformat(d.isoformat() + _TZ_NOON_UTC_OFFSET)


@dataclass(frozen=True)
class WheelStrikeSelectionRule:
    """A plain, deterministic strike/expiration picker for the backtest
    -- not a re-implementation of `src.wheel.eligibility`'s full
    screening or the live Risk Engine (this module is about proving the
    Wheel's *cross-cycle statefulness* is correct; the live path already
    gates entries through the full eligibility/Quant/Risk pipeline)."""

    min_dte: int
    max_dte: int
    target_delta_low: float
    target_delta_high: float
    contracts: int
    limit_price_slippage_pct: float = 0.10  # accept a limit up to this much worse than mid, so a realistic fill can occur


@dataclass(frozen=True)
class WheelBacktestConfig:
    initial_cash: float
    fill_config: PaperBrokerConfig
    commission_schedule: CommissionSchedule
    csp_rule: WheelStrikeSelectionRule
    cc_rule: WheelStrikeSelectionRule
    risk_free_rate: float = 0.04


@dataclass
class WheelBacktestState:
    cash: float
    wheel: WheelPosition
    wheel_history: list[WheelPosition] = field(default_factory=list)  # one snapshot per state transition, for audit


QuoteLookup = Callable[[str, date], list[HistoricalOptionQuote]]


def _select_strike(quotes: list[HistoricalOptionQuote], *, as_of: date, right: OptionRight, rule: WheelStrikeSelectionRule, rate: float) -> HistoricalOptionQuote | None:
    """Picks the quote whose Black-Scholes delta (computed from its own
    `iv`, never fabricated -- a quote with no `iv` is simply not a
    candidate) is closest to the midpoint of `[target_delta_low,
    target_delta_high]`, among quotes within the permitted DTE window.
    Returns `None` if nothing qualifies (a legitimate "no entry today,"
    not an error)."""
    target_mid = (rule.target_delta_low + rule.target_delta_high) / 2.0
    quant_right = QuantOptionRight.PUT if right == OptionRight.PUT else QuantOptionRight.CALL
    best: tuple[float, HistoricalOptionQuote] | None = None
    for q in quotes:
        if q.right != right or q.iv is None or q.iv <= 0 or q.mid <= 0:
            continue
        dte = (q.expiration - as_of).days
        if not (rule.min_dte <= dte <= rule.max_dte):
            continue
        t = dte / 365.0
        if t <= 0:
            continue
        d = abs(bs_delta(q.underlying_price, q.strike, t, rate, q.iv, quant_right))
        if not (rule.target_delta_low <= d <= rule.target_delta_high):
            continue
        distance = abs(d - target_mid)
        if best is None or distance < best[0]:
            best = (distance, q)
    return best[1] if best is not None else None


def open_csp_if_candidate(state: WheelBacktestState, *, as_of: date, quote_lookup: QuoteLookup, config: WheelBacktestConfig, proposal_id: str) -> WheelBacktestState:
    """No-op unless the Wheel is at `WHEEL_CANDIDATE` -- the caller
    drives the day loop and decides when a fresh candidate should even
    be considered (Part 10/16: a new CSP always competes again, never
    auto-reopened by this engine on its own)."""
    if state.wheel.state != WheelState.WHEEL_CANDIDATE:
        return state
    quotes = assert_no_lookahead_options(quote_lookup(state.wheel.ticker, as_of), as_of)
    chosen = _select_strike(quotes, as_of=as_of, right=OptionRight.PUT, rule=config.csp_rule, rate=config.risk_free_rate)
    if chosen is None:
        return state

    leg = BacktestLeg(right=OptionRight.PUT, strike=chosen.strike, side="sell")
    limit_price = chosen.mid * (1 - config.csp_rule.limit_price_slippage_pct)
    result = execute_entry(
        legs=[leg], expiration=chosen.expiration, quotes=[chosen], requested_contracts=config.csp_rule.contracts,
        limit_price=limit_price, fill_config=config.fill_config, commission_schedule=config.commission_schedule,
    )
    if result.filled_contracts <= 0:
        return state

    required_cash = chosen.strike * _CONTRACT_MULTIPLIER * result.filled_contracts
    if required_cash > state.cash:
        return state  # genuinely cash-secured -- never open beyond available cash (Part 3)

    new_wheel = lifecycle.open_csp(
        state.wheel, strike=chosen.strike, expiration=chosen.expiration, contracts=result.filled_contracts,
        premium_per_share=result.realistic_price / result.filled_contracts if result.filled_contracts else 0.0,
        commission=result.commission, proposal_id=proposal_id, position_id=None, now=_as_datetime(as_of),
    )
    new_cash = state.cash + result.realistic_price * _CONTRACT_MULTIPLIER - result.commission
    return WheelBacktestState(cash=new_cash, wheel=new_wheel, wheel_history=state.wheel_history + [new_wheel])


def open_cc_if_eligible(state: WheelBacktestState, *, as_of: date, quote_lookup: QuoteLookup, config: WheelBacktestConfig, proposal_id: str) -> WheelBacktestState:
    if state.wheel.state != WheelState.CC_ELIGIBLE:
        return state
    quotes = assert_no_lookahead_options(quote_lookup(state.wheel.ticker, as_of), as_of)
    chosen = _select_strike(quotes, as_of=as_of, right=OptionRight.CALL, rule=config.cc_rule, rate=config.risk_free_rate)
    max_contracts = state.wheel.accounting.shares_owned // _CONTRACT_MULTIPLIER
    if chosen is None or max_contracts <= 0:
        return WheelBacktestState(
            cash=state.cash,
            wheel=lifecycle.no_cc_trade(state.wheel, reason="no acceptable covered call strike found today", now=_as_datetime(as_of)),
            wheel_history=state.wheel_history,
        )

    contracts = min(config.cc_rule.contracts, max_contracts)
    leg = BacktestLeg(right=OptionRight.CALL, strike=chosen.strike, side="sell")
    limit_price = chosen.mid * (1 - config.cc_rule.limit_price_slippage_pct)
    result = execute_entry(
        legs=[leg], expiration=chosen.expiration, quotes=[chosen], requested_contracts=contracts,
        limit_price=limit_price, fill_config=config.fill_config, commission_schedule=config.commission_schedule,
    )
    if result.filled_contracts <= 0:
        return WheelBacktestState(
            cash=state.cash,
            wheel=lifecycle.no_cc_trade(state.wheel, reason="covered call candidate did not fill", now=_as_datetime(as_of)),
            wheel_history=state.wheel_history,
        )

    acq_basis = state.wheel.accounting.acquisition_basis_per_share
    econ_basis = state.wheel.accounting.economic_basis_per_share
    below_acq = acq_basis is not None and chosen.strike < acq_basis
    below_econ = econ_basis is not None and chosen.strike < econ_basis
    max_loss = max((acq_basis - chosen.strike) * _CONTRACT_MULTIPLIER * result.filled_contracts, 0.0) if acq_basis is not None else None

    new_wheel = lifecycle.open_cc(
        state.wheel, strike=chosen.strike, expiration=chosen.expiration, contracts=result.filled_contracts,
        premium_per_share=result.realistic_price / result.filled_contracts if result.filled_contracts else 0.0,
        commission=result.commission, proposal_id=proposal_id, position_id=None, now=_as_datetime(as_of),
        below_acquisition_basis=below_acq, below_economic_basis=below_econ, max_loss_if_called_away=max_loss,
    )
    new_cash = state.cash + result.realistic_price * _CONTRACT_MULTIPLIER - result.commission
    return WheelBacktestState(cash=new_cash, wheel=new_wheel, wheel_history=state.wheel_history + [new_wheel])


def _settle_open_option(state: WheelBacktestState, *, as_of: date, quote_lookup: QuoteLookup, right: OptionRight) -> WheelBacktestState:
    cycle = state.wheel.open_csp_cycle if right == OptionRight.PUT else state.wheel.open_cc_cycle
    if cycle is None or cycle.expiration != as_of:
        return state
    quotes = assert_no_lookahead_options(quote_lookup(state.wheel.ticker, as_of), as_of)
    settlement_quote = next((q for q in quotes if q.strike == cycle.strike and q.right == right and q.expiration == as_of), None)
    if settlement_quote is None:
        raise WheelBacktestDataInsufficientError(
            f"BACKTEST_DATA_INSUFFICIENT: no settlement quote for {state.wheel.ticker} "
            f"{right.value} {cycle.strike} expiring {as_of.isoformat()}"
        )
    settlement_price = settlement_quote.underlying_price
    leg = BacktestLeg(right=right, strike=cycle.strike, side="sell")
    [settled] = settle_position([leg], cycle.contracts, settlement_price)

    now = _as_datetime(as_of)
    if right == OptionRight.PUT:
        if settled.assigned_or_exercised:
            new_wheel = lifecycle.csp_assigned(state.wheel, now=now)
            # cash impact: pay the strike for the shares, exactly as PaperBroker.settle_expiration does
            new_cash = state.cash + settled.cash_impact
        else:
            new_wheel = lifecycle.csp_expires_worthless(state.wheel, now=now)
            new_cash = state.cash
    else:
        if settled.assigned_or_exercised:
            new_wheel = lifecycle.shares_called_away(state.wheel, now=now)
            new_cash = state.cash + settled.cash_impact
        else:
            new_wheel = lifecycle.cc_expires_worthless(state.wheel, now=now)
            new_cash = state.cash

    return WheelBacktestState(cash=new_cash, wheel=new_wheel, wheel_history=state.wheel_history + [new_wheel])


def advance_one_day(state: WheelBacktestState, *, as_of: date, quote_lookup: QuoteLookup, config: WheelBacktestConfig, day_index: int) -> WheelBacktestState:
    """One full day of the Wheel backtest loop: settle any expiring
    option first (a day cannot both expire and re-enter on the same
    tick without first resolving what happened to the position that
    just expired), then attempt whichever entry is relevant to the
    Wheel's current state."""
    proposal_id = f"bt-wheel-{state.wheel.wheel_id}-{day_index}"

    state = _settle_open_option(state, as_of=as_of, quote_lookup=quote_lookup, right=OptionRight.PUT)
    state = _settle_open_option(state, as_of=as_of, quote_lookup=quote_lookup, right=OptionRight.CALL)

    if state.wheel.state == WheelState.ASSIGNED_SHARES:
        state = WheelBacktestState(
            cash=state.cash, wheel=lifecycle.mark_cc_eligible(state.wheel, now=_as_datetime(as_of)), wheel_history=state.wheel_history,
        )
    elif state.wheel.state in (WheelState.CC_EXPIRED, WheelState.CC_CLOSED):
        state = WheelBacktestState(
            cash=state.cash, wheel=lifecycle.mark_cc_eligible(state.wheel, now=_as_datetime(as_of)), wheel_history=state.wheel_history,
        )
    elif state.wheel.state == WheelState.SHARES_CALLED_AWAY:
        state = WheelBacktestState(
            cash=state.cash, wheel=lifecycle.complete_wheel(state.wheel, now=_as_datetime(as_of)), wheel_history=state.wheel_history,
        )

    state = open_csp_if_candidate(state, as_of=as_of, quote_lookup=quote_lookup, config=config, proposal_id=proposal_id)
    state = open_cc_if_eligible(state, as_of=as_of, quote_lookup=quote_lookup, config=config, proposal_id=proposal_id)
    return state


def run_wheel_backtest(
    *, wheel_id: str, ticker: str, start: date, trading_days: list[date], quote_lookup: QuoteLookup, config: WheelBacktestConfig,
) -> WheelBacktestState:
    """Drives one Wheel through a full historical window, one calendar
    day at a time, in order. `trading_days` must be sorted ascending
    (same requirement `src.backtest.engine.run_backtest` enforces) --
    the day-by-day loop is the entire mechanism preventing look-ahead
    here, so processing out of order would silently defeat it."""
    if trading_days != sorted(trading_days):
        raise ValueError("trading_days must be sorted ascending")
    wheel = lifecycle.open_wheel_candidate(wheel_id=wheel_id, ticker=ticker, now=_as_datetime(start))
    state = WheelBacktestState(cash=config.initial_cash, wheel=wheel, wheel_history=[wheel])
    for i, as_of in enumerate(trading_days):
        state = advance_one_day(state, as_of=as_of, quote_lookup=quote_lookup, config=config, day_index=i)
        if state.wheel.state in (WheelState.WHEEL_COMPLETE, WheelState.WHEEL_EXITED, WheelState.WHEEL_HALTED, WheelState.WHEEL_REJECTED):
            break
    return state
