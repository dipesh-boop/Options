"""Wheel-specific risk analytics (Part 12).

Nothing here is a second Risk Engine, and nothing here grants the Wheel
any exemption from portfolio limits: every actual CSP/CC order a Wheel
places is still independently evaluated in full by
`src.risk.engine.evaluate_trade_proposal`, exactly like every other
proposal in this platform, using the Wheel's actual `capital_required`
(the same `check_underlying_concentration`/`check_sector_concentration`
Part 12 requires stay authoritative). This module answers a different,
Wheel-specific question: for a Wheel already open, what does its true
downside exposure look like, since assignment converts option premium
into real equity ownership that plain strike-distance or premium-
collected figures do not capture on their own (Part 12: "Do not
reinterpret undefined Wheel downside as only the option premium or
strike distance... recognize that assignment creates equity downside
exposure").
"""
from __future__ import annotations

from dataclasses import dataclass

from src.quant.black_scholes import Leg as QuantLeg
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.quant.black_scholes import Side as QuantSide
from src.quant.monte_carlo import Position, stress_test
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct
from src.wheel.models import WheelPosition
from src.wheel.state import WheelState

# Part 12's explicit shock grid for CSP/Wheel risk analytics -- distinct
# from src.risk.stress.STRESS_SPOT_SHOCKS (the platform's general
# six-shock grid, symmetric +-5/10/20%): a Wheel's live risk is entirely
# on the downside once a put is short or shares are held, so Part 12
# names five down-only shocks reaching to -50%, deeper than the general
# grid goes at all.
WHEEL_STRESS_SPOT_SHOCKS: tuple[float, ...] = (-0.05, -0.10, -0.20, -0.30, -0.50)
WHEEL_STRESS_VOL_SHOCKS: tuple[float, ...] = (0.10, 0.25, 0.50)

_CONTRACT_MULTIPLIER = 100

_STATES_WITH_OPEN_SHORT_PUT = frozenset({WheelState.CSP_OPEN})
_STATES_HOLDING_SHARES = frozenset(
    {WheelState.ASSIGNED_SHARES, WheelState.CC_ELIGIBLE, WheelState.CC_OPEN, WheelState.CC_EXPIRED, WheelState.CC_CLOSED}
)
_STATES_WITH_OPEN_SHORT_CALL = frozenset({WheelState.CC_OPEN})


def current_wheel_quant_position(wheel: WheelPosition) -> Position | None:
    """The Position this Wheel currently, actually holds -- a short put
    while `CSP_OPEN`, shares (plus a short call if one is open) while
    holding stock, or `None` for a candidate/terminal Wheel with no live
    exposure at all. Never a hypothetical or forward-looking position:
    exactly what `wheel.accounting`/`wheel.state` say is on the books
    right now."""
    if wheel.state in _STATES_WITH_OPEN_SHORT_PUT:
        cycle = wheel.open_csp_cycle
        if cycle is None:
            return None
        leg = QuantLeg(
            right=QuantOptionRight.PUT, strike=cycle.strike, side=QuantSide.SELL,
            entry_price=cycle.premium_received_per_share, quantity=cycle.contracts,
        )
        return Position(legs=[leg])

    if wheel.state in _STATES_HOLDING_SHARES:
        legs: list[QuantLeg] = []
        if wheel.state in _STATES_WITH_OPEN_SHORT_CALL:
            cc = wheel.open_cc_cycle
            if cc is not None:
                legs.append(
                    QuantLeg(
                        right=QuantOptionRight.CALL, strike=cc.strike, side=QuantSide.SELL,
                        entry_price=cc.premium_received_per_share, quantity=cc.contracts,
                    )
                )
        basis = wheel.accounting.acquisition_basis_per_share or 0.0
        return Position(legs=legs, underlying_shares=wheel.accounting.shares_owned, underlying_cost_basis=basis)

    return None


@dataclass(frozen=True)
class WheelStressScenario:
    spot_shock_pct: float
    vol_shock_pct: float
    pnl: float
    loss_pct_of_capital_committed: float


@dataclass(frozen=True)
class WheelStressResult:
    scenarios: tuple[WheelStressScenario, ...]
    worst_case_loss: float
    worst_case_loss_pct_of_capital_committed: float


def stress_test_wheel(wheel: WheelPosition, *, spot: float, sigma: float, t: float, rate: float) -> WheelStressResult:
    """Reprices whatever this Wheel currently holds under Part 12's
    required shock grid. A Wheel with no live position (a fresh
    candidate, or any terminal state) has nothing to stress and returns
    an all-empty, zero-loss result -- correct, not an omission."""
    position = current_wheel_quant_position(wheel)
    if position is None or (not position.legs and position.underlying_shares == 0):
        return WheelStressResult(scenarios=(), worst_case_loss=0.0, worst_case_loss_pct_of_capital_committed=0.0)

    raw_scenarios = stress_test(position, spot, sigma, t, rate, spot_shocks=WHEEL_STRESS_SPOT_SHOCKS, vol_shocks=WHEEL_STRESS_VOL_SHOCKS)
    capital = max(wheel.accounting.capital_committed, wheel.accounting.max_capital_committed, 1e-9)
    scenarios = tuple(
        WheelStressScenario(
            spot_shock_pct=s.spot_shock_pct, vol_shock_pct=s.vol_shock_pct, pnl=s.pnl,
            loss_pct_of_capital_committed=max(-s.pnl, 0.0) / capital,
        )
        for s in raw_scenarios
    )
    worst = max((-s.pnl for s in raw_scenarios), default=0.0)
    return WheelStressResult(scenarios=scenarios, worst_case_loss=worst, worst_case_loss_pct_of_capital_committed=worst / capital)


@dataclass(frozen=True)
class WheelAggregateExposure:
    """Part 12: "Wheel exposure must aggregate: reserved CSP cash; owned
    shares; covered-call obligations; correlated portfolio exposure." A
    single figure (`total_capital_at_risk`) that a caller can feed into
    the *same* `check_underlying_concentration`/`check_sector_concentration`
    functions every other strategy's capital-at-risk goes through -- the
    Wheel never gets a separate, looser concentration rule."""

    reserved_csp_cash: float
    shares_market_value: float
    covered_call_obligation_shares: int
    total_capital_at_risk: float
    underlying_exposure_pct_of_nav: float
    sector_exposure_pct_of_nav: float


def compute_wheel_aggregate_exposure(
    wheel: WheelPosition, *, current_underlying_price: float, portfolio: Portfolio, sector: str, limits: RiskLimitsConfig,
) -> WheelAggregateExposure:
    reserved_cash = wheel.accounting.capital_committed if wheel.state == WheelState.CSP_OPEN else 0.0
    shares_value = wheel.accounting.shares_owned * current_underlying_price if wheel.accounting.shares_owned > 0 else 0.0
    cc_obligation_shares = wheel.open_cc_cycle.contracts * _CONTRACT_MULTIPLIER if wheel.open_cc_cycle is not None else 0

    # Total capital at risk: while a put is short, the full cash-secured
    # amount is what's actually exposed if assigned; once shares are
    # held, it's their current market value (the covered call caps
    # upside, it doesn't reduce downside exposure below the shares'
    # own value, so it is never subtracted here).
    total_at_risk = reserved_cash + shares_value

    return WheelAggregateExposure(
        reserved_csp_cash=reserved_cash,
        shares_market_value=shares_value,
        covered_call_obligation_shares=cc_obligation_shares,
        total_capital_at_risk=total_at_risk,
        underlying_exposure_pct_of_nav=underlying_exposure_pct(portfolio, wheel.ticker, total_at_risk),
        sector_exposure_pct_of_nav=sector_exposure_pct(portfolio, sector, total_at_risk),
    )
