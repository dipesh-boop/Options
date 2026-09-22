"""Deterministic PAPER portfolio revaluation (Step 22.4 Part 14).

Every number here is computed once, in Python, from the same
authoritative building blocks the Risk Engine itself uses --
`src.quant.volatility.implied_volatility` (never a provider's own
reported IV) feeding `src.quant.greeks.all_greeks`/`net_greeks` -- so the
control loop's live P&L/Greeks view can never silently diverge from what
`src.risk.trade_risk` would compute for the same position (Part 29: "no
LLM ever computes an authoritative price, Greek, probability, or risk
figure" implies the control loop doesn't invent its own either, it calls
the same Quant functions everything else does).

Missing means missing (Part 6): a position whose current contracts
can't be matched, whose match is stale, or whose underlying price is
unavailable is reported with `PositionValuationStatus.DATA_INSUFFICIENT`
and a `reason`, never a fabricated $0 P&L or silently-stale Greeks.
`revalue_portfolio` isolates that per position, exactly like
`src.data.quality_gate` isolates one bad contract from an otherwise-good
chain -- one position's missing data doesn't blank out the whole
portfolio's aggregate figures, but it does exclude that position from
them and it is never silently absent from `positions_data_insufficient`.

Deliberately out of scope: `PortfolioPosition`/`PortfolioPositionLeg`
(src.risk.portfolio_risk) carry no `quantity_ratio` per leg the way
`src.llm.schemas.OptionLeg` does for a *proposed* trade -- every
existing tracked position leg is implicitly 1:1 with `PortfolioPosition
.contracts`. Generalizing the Risk Engine's own portfolio-state schema
to carry a per-leg ratio for already-open multi-leg positions is a
pre-existing gap this module does not attempt to close (it was out of
scope for Step 20A too, which only extended the *proposal* schema); this
module reads `PortfolioPositionLeg` exactly as `src.risk.stress` already
does.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.data.option_chain import OptionContract
from src.data.provider import DEFAULT_MAX_QUOTE_AGE, FreshnessStatus
from src.quant.black_scholes import MIN_T, OptionRight
from src.quant.greeks import Greeks, all_greeks
from src.quant.volatility import implied_volatility
from src.risk.drawdown import DrawdownZone, current_drawdown_pct, drawdown_zone
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import (
    Portfolio,
    PortfolioPosition,
    capital_deployed_pct,
    cash_reserve_pct,
)

_CONTRACT_MULTIPLIER = 100


class PositionValuationStatus(str, Enum):
    OK = "ok"
    DATA_INSUFFICIENT = "data_insufficient"


class LegValuation(BaseModel):
    """One leg's own mark and (if solvable) Greeks. `mark_price` is the
    contract's own `mid` -- the actual tradable two-sided price -- never
    a Black-Scholes theoretical price; Quant only supplies the Greeks,
    which do require re-deriving an IV from that mark (Part 29's
    "authoritative Greeks", never a provider's own reported ones)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    right: Literal["C", "P"]
    side: Literal["buy", "sell"]
    strike: float
    entry_price: float
    mark_price: float | None = None
    implied_vol: float | None = None
    greeks: tuple[float, float, float, float] | None = None  # (delta, gamma, theta, vega) per share
    issue: str | None = None


class PositionValuation(BaseModel):
    """One position's full revaluation. `status=DATA_INSUFFICIENT` means
    at least one leg couldn't be matched/priced/freshness-checked --
    `unrealized_pnl`/Greeks are then `None`, never a partial or
    zero-filled guess, per Part 6."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str
    ticker: str
    status: PositionValuationStatus
    reason: str | None = None
    dte: int | None = None
    underlying_price: float | None = None
    unrealized_pnl: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    legs: tuple[LegValuation, ...] = ()


ContractIndex = dict[tuple, OptionContract]  # (expiration: date, strike: float, right: "C"|"P") -> contract


def build_contract_index(contracts: list[OptionContract]) -> ContractIndex:
    """Indexes an already quality-gated contract list
    (`src.data.quality_gate.validate_option_chain(...).valid_contracts`)
    by `(expiration, strike, right)` for O(1) leg lookup. The caller is
    responsible for having already run the quality gate -- this function
    does not re-validate, it only indexes whatever it's given; a
    duplicate key keeps the first contract seen (chain fetches should
    never contain true duplicates, but this function must not raise if
    one does)."""
    index: ContractIndex = {}
    for contract in contracts:
        key = (contract.expiration, contract.strike, contract.right.value)
        index.setdefault(key, contract)
    return index


def revalue_position(
    position: PortfolioPosition,
    *,
    underlying_price: float | None,
    contract_index: ContractIndex,
    as_of: datetime,
    rate: float,
    max_quote_age: timedelta = DEFAULT_MAX_QUOTE_AGE,
) -> PositionValuation:
    """Revalues one open position from the current market snapshot.
    Pure -- never raises, never touches I/O; every failure mode
    (unmatched leg, stale leg, missing underlying price, an IV that
    can't be solved) is reported on the returned object rather than
    thrown, so one bad position can be isolated by
    `revalue_portfolio`'s caller without an exception handler around
    every position."""
    if underlying_price is None or underlying_price <= 0:
        return PositionValuation(
            position_id=position.position_id,
            ticker=position.ticker,
            status=PositionValuationStatus.DATA_INSUFFICIENT,
            reason="no current underlying price available",
            dte=max((position.expiration - as_of.date()).days, 0),
        )

    dte = max((position.expiration - as_of.date()).days, 0)
    t = max(dte / 365.0, MIN_T)

    leg_valuations: list[LegValuation] = []
    any_leg_bad = False
    total_pnl = 0.0
    net_delta = net_gamma = net_theta = net_vega = 0.0
    all_greeks_known = True

    for leg in position.legs:
        contract = contract_index.get((position.expiration, leg.strike, leg.right))
        if contract is None:
            leg_valuations.append(
                LegValuation(
                    right=leg.right, side=leg.side, strike=leg.strike, entry_price=leg.entry_price,
                    issue="no matching contract in current market data",
                )
            )
            any_leg_bad = True
            continue
        if contract.freshness_status(as_of, max_quote_age) == FreshnessStatus.STALE:
            leg_valuations.append(
                LegValuation(
                    right=leg.right, side=leg.side, strike=leg.strike, entry_price=leg.entry_price,
                    mark_price=contract.mid,
                    issue=f"quote is {contract.age(as_of)} old, exceeding max allowed age {max_quote_age}",
                )
            )
            any_leg_bad = True
            continue

        mark = contract.mid
        right = OptionRight(leg.right)
        sign = 1.0 if leg.side == "buy" else -1.0
        total_pnl += sign * (mark - leg.entry_price) * position.contracts * _CONTRACT_MULTIPLIER

        iv = implied_volatility(mark, underlying_price, leg.strike, t, rate, right)
        greeks_tuple: tuple[float, float, float, float] | None = None
        issue: str | None = None
        if iv is None:
            all_greeks_known = False
            issue = "implied volatility could not be solved from the current mark -- Greeks unavailable"
        else:
            g: Greeks = all_greeks(underlying_price, leg.strike, t, rate, iv, right)
            weight = sign * position.contracts * _CONTRACT_MULTIPLIER
            net_delta += weight * g.delta
            net_gamma += weight * g.gamma
            net_theta += weight * g.theta
            net_vega += weight * g.vega
            greeks_tuple = (g.delta, g.gamma, g.theta, g.vega)

        leg_valuations.append(
            LegValuation(
                right=leg.right, side=leg.side, strike=leg.strike, entry_price=leg.entry_price,
                mark_price=mark, implied_vol=iv, greeks=greeks_tuple, issue=issue,
            )
        )

    if any_leg_bad:
        return PositionValuation(
            position_id=position.position_id, ticker=position.ticker,
            status=PositionValuationStatus.DATA_INSUFFICIENT,
            reason="one or more legs have no fresh, matching market quote",
            dte=dte, underlying_price=underlying_price, legs=tuple(leg_valuations),
        )

    return PositionValuation(
        position_id=position.position_id, ticker=position.ticker,
        status=PositionValuationStatus.OK,
        dte=dte, underlying_price=underlying_price, unrealized_pnl=total_pnl,
        delta=net_delta if all_greeks_known else None,
        gamma=net_gamma if all_greeks_known else None,
        theta=net_theta if all_greeks_known else None,
        vega=net_vega if all_greeks_known else None,
        legs=tuple(leg_valuations),
    )


class PortfolioValuationResult(BaseModel):
    """The control loop's full Part 14 output for one cycle. `is_complete
    =False` means at least one position is in `positions_data_insufficient`
    -- every aggregate figure below is computed only from the positions
    that DID revalue successfully, and is explicitly partial when that
    list is non-empty (never silently treated as the whole portfolio)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    nav: float
    cash: float
    buying_power_proxy: float  # this paper account carries no margin -- proxy is simply free cash
    capital_deployed_pct: float
    cash_reserve_pct: float
    current_drawdown_pct: float
    drawdown_zone: DrawdownZone
    positions: tuple[PositionValuation, ...]
    positions_data_insufficient: tuple[str, ...]
    is_complete: bool
    total_unrealized_pnl_known: float | None  # None only when zero positions are open
    portfolio_delta: float | None
    portfolio_gamma: float | None
    portfolio_theta: float | None
    portfolio_vega: float | None


def revalue_portfolio(
    portfolio: Portfolio,
    *,
    underlying_prices: dict[str, float],
    contracts_by_ticker: dict[str, list[OptionContract]],
    limits: RiskLimitsConfig,
    as_of: datetime,
    max_quote_age: timedelta = DEFAULT_MAX_QUOTE_AGE,
) -> PortfolioValuationResult:
    """Revalues every open position and aggregates the portfolio-level
    Part 14/15 figures that don't require a proposed trade to compute
    (NAV/cash/drawdown reuse the existing `src.risk.portfolio_risk`/
    `src.risk.drawdown` helpers rather than recomputing them; this
    function's own job is the position-by-position mark-to-market loop
    those helpers don't do).

    `underlying_prices`/`contracts_by_ticker` are supplied by the
    caller (the control loop, after running `src.data.quality_gate` on
    whatever `src.data.tradier_provider` returned) rather than fetched
    here -- this module has no market-data connection of its own, the
    same separation `src.dashboard.service`'s own docstring establishes.
    """
    indices = {ticker: build_contract_index(contracts) for ticker, contracts in contracts_by_ticker.items()}

    valuations: list[PositionValuation] = []
    for position in portfolio.positions:
        valuations.append(
            revalue_position(
                position,
                underlying_price=underlying_prices.get(position.ticker),
                contract_index=indices.get(position.ticker, {}),
                as_of=as_of,
                rate=limits.risk_free_rate,
                max_quote_age=max_quote_age,
            )
        )

    ok_valuations = [v for v in valuations if v.status == PositionValuationStatus.OK]
    insufficient_ids = tuple(v.position_id for v in valuations if v.status == PositionValuationStatus.DATA_INSUFFICIENT)

    total_pnl = sum(v.unrealized_pnl for v in ok_valuations) if ok_valuations else None
    greeks_known = [v for v in ok_valuations if v.delta is not None]
    portfolio_delta = sum(v.delta for v in greeks_known) if greeks_known else None
    portfolio_gamma = sum(v.gamma for v in greeks_known) if greeks_known else None
    portfolio_theta = sum(v.theta for v in greeks_known) if greeks_known else None
    portfolio_vega = sum(v.vega for v in greeks_known) if greeks_known else None

    dd_pct = current_drawdown_pct(portfolio)

    return PortfolioValuationResult(
        as_of=as_of,
        nav=portfolio.nav,
        cash=portfolio.cash,
        buying_power_proxy=portfolio.cash,
        capital_deployed_pct=capital_deployed_pct(portfolio),
        cash_reserve_pct=cash_reserve_pct(portfolio),
        current_drawdown_pct=dd_pct,
        drawdown_zone=drawdown_zone(dd_pct, limits),
        positions=tuple(valuations),
        positions_data_insufficient=insufficient_ids,
        is_complete=len(insufficient_ids) == 0,
        total_unrealized_pnl_known=total_pnl,
        portfolio_delta=portfolio_delta,
        portfolio_gamma=portfolio_gamma,
        portfolio_theta=portfolio_theta,
        portfolio_vega=portfolio_vega,
    )
