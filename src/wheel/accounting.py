"""Pure Wheel accounting functions (Part 6/9). Every function here is a
deterministic transformation of already-known numbers (fill prices,
strikes, commissions, contract counts) — none of them call the network,
touch a clock other than a caller-supplied `now`, or accept an LLM
output. `WheelAccounting` (the durable ledger) is updated only through
these functions, never by a caller setting a field directly, so
`gross_stock_acquisition_cost`/`acquisition_basis_per_share`/etc. can
never drift from what actually happened on the books.

**Two distinct bases, always kept apart** (Part 6's explicit
requirement): `acquisition_basis_per_share` is the tax/accounting-style
cost — the strike price paid at assignment, full stop, exactly what a
brokerage 1099-B would show. `economic_basis_per_share` is the
strategy-analysis figure: acquisition basis reduced by every dollar of
net Wheel premium collected per share currently held. A report that
blends the two together (e.g. showing only economic basis as "your cost")
would understate a real embedded loss — every rendering in this codebase
(`src.wheel.reporting`, the dashboard) must show both, never one
disguised as the other.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.wheel.models import WheelAccounting

_CONTRACT_MULTIPLIER = 100


def _round2(x: float) -> float:
    return round(x, 2)


def csp_opened(accounting: WheelAccounting, *, strike: float, contracts: int) -> WheelAccounting:
    """A CSP is genuinely cash-secured (Part 3): opening one reserves
    `strike * 100 * contracts` of capital immediately, before any
    premium is even known."""
    reserved = strike * _CONTRACT_MULTIPLIER * contracts
    new_committed = accounting.capital_committed + reserved
    return accounting.model_copy(
        update={
            "capital_committed": _round2(new_committed),
            "max_capital_committed": _round2(max(accounting.max_capital_committed, new_committed)),
        }
    )


def csp_premium_received(accounting: WheelAccounting, *, premium_per_share: float, contracts: int, commission: float) -> WheelAccounting:
    return accounting.model_copy(
        update={
            "total_csp_premium": _round2(accounting.total_csp_premium + premium_per_share * _CONTRACT_MULTIPLIER * contracts),
            "total_commissions": _round2(accounting.total_commissions + commission),
        }
    )


def csp_released_unassigned(accounting: WheelAccounting, *, strike: float, contracts: int, realized_pnl: float) -> WheelAccounting:
    """The put expired worthless or was bought back before assignment —
    the reserved cash is released back to the buying-power pool (Part
    10: "release reserved cash")."""
    reserved = strike * _CONTRACT_MULTIPLIER * contracts
    return accounting.model_copy(
        update={
            "capital_committed": _round2(max(accounting.capital_committed - reserved, 0.0)),
            "realized_option_pnl": _round2(accounting.realized_option_pnl + realized_pnl),
        }
    )


def assigned(accounting: WheelAccounting, *, strike: float, contracts: int, assigned_at: datetime) -> WheelAccounting:
    """CSP assignment (Part 6): the reserved cash becomes shares at cost.
    `acquisition_basis_per_share` is a weighted average across every
    assignment this Wheel has ever had (normally exactly one, but the
    formula is correct even if a future research config allows multiple
    separately-approved CSP cycles before the first assignment)."""
    new_shares = _CONTRACT_MULTIPLIER * contracts
    new_gross_cost = strike * new_shares
    total_shares = accounting.shares_owned + new_shares
    total_gross_cost = accounting.gross_stock_acquisition_cost + new_gross_cost
    new_basis = total_gross_cost / total_shares if total_shares > 0 else None
    return accounting.model_copy(
        update={
            "shares_owned": total_shares,
            "gross_stock_acquisition_cost": _round2(total_gross_cost),
            "acquisition_basis_per_share": _round2(new_basis) if new_basis is not None else None,
            "economic_basis_per_share": _round2(compute_economic_basis_per_share(new_basis, accounting.total_csp_premium, total_shares)) if new_basis is not None else None,
            "shares_acquired_at": accounting.shares_acquired_at or assigned_at,
            # Capital committed does not change here: reserved cash and
            # the shares it just bought are the same dollar amount --
            # only its *form* changed, from cash reservation to equity.
        }
    )


def compute_economic_basis_per_share(acquisition_basis_per_share: float | None, net_premium_dollars: float, shares_owned: int) -> float | None:
    """Acquisition basis reduced by net Wheel premium collected per
    share currently held. `net_premium_dollars` is every dollar of CSP +
    CC premium collected (minus commissions, if the caller chooses to
    net them in) up to this point in the Wheel's life -- callers pass
    whichever total is appropriate for the recompute point (see
    `cc_premium_received` below, which recomputes using the running
    CSP+CC total)."""
    if acquisition_basis_per_share is None or shares_owned <= 0:
        return None
    return acquisition_basis_per_share - (net_premium_dollars / shares_owned)


def cc_opened(accounting: WheelAccounting) -> WheelAccounting:
    """Selling a covered call reserves no additional cash (Part 7: it is
    covered entirely by shares already owned) -- a no-op on the ledger
    beyond the eligibility check `src.wheel.lifecycle` already performed
    (`shares_owned >= 100 * call_contracts`)."""
    return accounting


def cc_premium_received(accounting: WheelAccounting, *, premium_per_share: float, contracts: int, commission: float) -> WheelAccounting:
    new_cc_premium = accounting.total_cc_premium + premium_per_share * _CONTRACT_MULTIPLIER * contracts
    net_premium = accounting.total_csp_premium + new_cc_premium
    new_basis = compute_economic_basis_per_share(accounting.acquisition_basis_per_share, net_premium, accounting.shares_owned)
    return accounting.model_copy(
        update={
            "total_cc_premium": _round2(new_cc_premium),
            "total_commissions": _round2(accounting.total_commissions + commission),
            "economic_basis_per_share": _round2(new_basis) if new_basis is not None else None,
        }
    )


def cc_closed_unassigned(accounting: WheelAccounting, *, realized_pnl: float) -> WheelAccounting:
    """CC expired worthless or was bought back -- shares stay put, only
    the option-cycle P&L realizes."""
    return accounting.model_copy(update={"realized_option_pnl": _round2(accounting.realized_option_pnl + realized_pnl)})


def called_away(accounting: WheelAccounting, *, strike: float, contracts: int) -> WheelAccounting:
    """Shares assigned away on a covered call (Part 9): realize the
    stock leg's P&L against `acquisition_basis_per_share` (the
    tax-style basis -- economic basis is a strategy-analysis figure,
    never substituted for the real cost basis when computing what was
    actually gained or lost on the shares themselves), release that
    capital, and reduce the share count."""
    if accounting.acquisition_basis_per_share is None:
        raise ValueError("cannot realize called-away shares with no recorded acquisition basis")
    shares_sold = _CONTRACT_MULTIPLIER * contracts
    stock_pnl = (strike - accounting.acquisition_basis_per_share) * shares_sold
    cost_of_sold_shares = accounting.acquisition_basis_per_share * shares_sold
    remaining_shares = accounting.shares_owned - shares_sold
    return accounting.model_copy(
        update={
            "shares_owned": max(remaining_shares, 0),
            "gross_stock_acquisition_cost": _round2(max(accounting.gross_stock_acquisition_cost - cost_of_sold_shares, 0.0)),
            "realized_stock_pnl": _round2(accounting.realized_stock_pnl + stock_pnl),
            "capital_committed": _round2(max(accounting.capital_committed - cost_of_sold_shares, 0.0)),
        }
    )


@dataclass(frozen=True)
class WheelEconomicsSummary:
    """Every metric Part 9 (per-Wheel) requires, assembled in one place
    so the dashboard, Fidelity ticket context, and validation reporting
    (`src.wheel.reporting`) all read the exact same numbers rather than
    three independent partial recomputations."""

    wheel_id: str
    ticker: str
    state: str
    shares_owned: int
    acquisition_basis_per_share: float | None
    economic_basis_per_share: float | None
    current_underlying_price: float | None
    current_market_value: float | None
    total_csp_premium: float
    total_cc_premium: float
    total_premium: float
    total_commissions: float
    realized_stock_pnl: float
    unrealized_stock_pnl: float
    realized_option_pnl: float
    total_net_pnl: float
    capital_committed: float
    max_capital_committed: float
    return_on_committed_capital: float | None
    annualized_return_on_committed_capital: float | None
    days_in_wheel: int
    days_holding_stock: int
    csp_cycle_count: int
    cc_cycle_count: int


def summarize_wheel_economics(wheel, *, current_underlying_price: float | None, now: datetime) -> WheelEconomicsSummary:
    """`wheel` is a `src.wheel.models.WheelPosition` -- accepted as a
    loosely-typed positional argument (not imported by name) purely to
    avoid a circular import between `models.py` and this module; every
    attribute accessed below is part of `WheelPosition`'s own public
    shape."""
    acc = wheel.accounting
    if wheel.started_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("started_at and now must both be timezone-aware")
    end_of_window = wheel.completed_at or now
    days_in_wheel = max((end_of_window - wheel.started_at).days, 0)
    days_holding_stock = max((end_of_window - acc.shares_acquired_at).days, 0) if acc.shares_acquired_at else 0

    current_market_value = (current_underlying_price * acc.shares_owned) if (current_underlying_price is not None and acc.shares_owned > 0) else None
    unrealized_stock_pnl = 0.0
    if current_underlying_price is not None and acc.acquisition_basis_per_share is not None and acc.shares_owned > 0:
        unrealized_stock_pnl = (current_underlying_price - acc.acquisition_basis_per_share) * acc.shares_owned

    total_premium = acc.total_csp_premium + acc.total_cc_premium
    total_net_pnl = total_premium - acc.total_commissions + acc.realized_stock_pnl + unrealized_stock_pnl

    roc = total_net_pnl / acc.max_capital_committed if acc.max_capital_committed > 0 else None
    annualized_roc = roc * (365.0 / days_in_wheel) if (roc is not None and days_in_wheel > 0) else roc

    return WheelEconomicsSummary(
        wheel_id=wheel.wheel_id,
        ticker=wheel.ticker,
        state=wheel.state.value,
        shares_owned=acc.shares_owned,
        acquisition_basis_per_share=acc.acquisition_basis_per_share,
        economic_basis_per_share=acc.economic_basis_per_share,
        current_underlying_price=current_underlying_price,
        current_market_value=_round2(current_market_value) if current_market_value is not None else None,
        total_csp_premium=_round2(acc.total_csp_premium),
        total_cc_premium=_round2(acc.total_cc_premium),
        total_premium=_round2(total_premium),
        total_commissions=_round2(acc.total_commissions),
        realized_stock_pnl=_round2(acc.realized_stock_pnl),
        unrealized_stock_pnl=_round2(unrealized_stock_pnl),
        realized_option_pnl=_round2(acc.realized_option_pnl),
        total_net_pnl=_round2(total_net_pnl),
        capital_committed=_round2(acc.capital_committed),
        max_capital_committed=_round2(acc.max_capital_committed),
        return_on_committed_capital=roc,
        annualized_return_on_committed_capital=annualized_roc,
        days_in_wheel=days_in_wheel,
        days_holding_stock=days_holding_stock,
        csp_cycle_count=len(wheel.csp_cycles),
        cc_cycle_count=len(wheel.cc_cycles),
    )


def below_basis_flags(call_strike: float, *, acquisition_basis_per_share: float | None, economic_basis_per_share: float | None) -> tuple[bool, bool]:
    """Part 8's deterministic below-basis check. Returns
    `(below_acquisition_basis, below_economic_basis)`. Never forbids
    anything on its own -- `src.wheel.lifecycle.propose_covered_call`
    attaches these flags to the `CcCycle`/proposal context for the
    Portfolio Manager and Risk Engine to see, per Part 8's "should NOT
    automatically be forbidden... but must require explicit deterministic
    justification"."""
    below_acq = acquisition_basis_per_share is not None and call_strike < acquisition_basis_per_share
    below_econ = economic_basis_per_share is not None and call_strike < economic_basis_per_share
    return below_acq, below_econ


def max_loss_if_called_away(call_strike: float, acquisition_basis_per_share: float, shares: int) -> float:
    """Part 8: "Track the maximum loss if the shares are called away at
    the proposed strike." A positive return means a real loss versus the
    tax-style acquisition basis (0 or negative means the strike is at or
    above basis, i.e. no loss on the stock leg if called away there)."""
    return max((acquisition_basis_per_share - call_strike) * shares, 0.0)
