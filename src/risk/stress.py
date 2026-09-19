"""Portfolio-level stress testing: deterministic mark-to-market impact
of the whole portfolio, including the proposed new trade, under
underlying and volatility shocks.

Wraps `src.quant.monte_carlo.stress_test` (single-position, deterministic
Black-Scholes repricing) once per existing position plus once for the
proposed trade, then aggregates across positions per scenario — this
module adds no pricing math of its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.quant.black_scholes import Leg as QuantLeg
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.quant.black_scholes import Side as QuantSide
from src.quant.greeks import net_greeks
from src.quant.monte_carlo import Position, stress_test
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, capital_deployed_pct

# The exact scenarios named in the platform spec: underlying moves of
# -20/-10/-5/+5/+10/+20%, volatility shocks of +10/+25/+50%. Distinct
# from src.quant.monte_carlo.STANDARD_VOL_SHOCKS, which serves a
# different (single-position, symmetric) default.
STRESS_SPOT_SHOCKS: tuple[float, ...] = (-0.20, -0.10, -0.05, 0.05, 0.10, 0.20)
STRESS_VOL_SHOCKS: tuple[float, ...] = (0.10, 0.25, 0.50)


class StressTestError(ValueError):
    """Raised when a position cannot be repriced under stress (e.g. no
    usable implied volatility) — treated as unknown risk, never silently
    excluded from the aggregate."""


@dataclass(frozen=True)
class PortfolioStressScenario:
    spot_shock_pct: float
    vol_shock_pct: float
    portfolio_pnl: float
    portfolio_loss_pct_of_nav: float  # positive number = a loss, as a fraction of current NAV
    net_delta_change: float
    net_vega_exposure: float
    capital_utilization_pct: float  # unaffected by the P&L scenario itself; included for context


@dataclass(frozen=True)
class PortfolioStressResult:
    scenarios: list[PortfolioStressScenario]
    worst_case_loss_pct_of_nav: float


def worst_case_stress_loss(position: Position, spot: float, sigma: float, t: float, rate: float) -> float:
    """The largest loss (a positive number; 0 if every scenario is
    profitable) a single position shows across the full required
    spot x vol shock grid. Used by `src.risk.engine` as a single-position
    worst case — see that module's stress-testing step for why it is not
    combined with a full multi-ticker portfolio reprice here (existing
    positions on other tickers are not repriceable from the market data
    the Risk Engine's declared inputs provide; their already-known
    max_loss is added statically instead, which is conservative since a
    position's max_loss is itself already its own worst case)."""
    scenarios = stress_test(position, spot, sigma, t, rate, spot_shocks=STRESS_SPOT_SHOCKS, vol_shocks=STRESS_VOL_SHOCKS)
    return max((-s.pnl for s in scenarios), default=0.0)


def _position_to_quant(position: PortfolioPosition) -> Position:
    legs = [
        QuantLeg(
            right=QuantOptionRight.CALL if leg.right == "C" else QuantOptionRight.PUT,
            strike=leg.strike,
            side=QuantSide.BUY if leg.side == "buy" else QuantSide.SELL,
            entry_price=leg.entry_price,
            quantity=position.contracts,
        )
        for leg in position.legs
    ]
    return Position(legs=legs, underlying_shares=position.underlying_shares)


def run_portfolio_stress_test(
    portfolio: Portfolio,
    *,
    new_position: Position | None,
    new_position_spot: float | None,
    new_position_sigma: float | None,
    new_position_t: float | None,
    spot_by_ticker: dict[str, float],
    sigma_by_ticker: dict[str, float],
    rate: float,
    t_by_ticker: dict[str, float],
    limits: RiskLimitsConfig,
) -> PortfolioStressResult:
    """Aggregates P&L, net delta change, and net vega exposure across
    every existing position plus (if supplied) the proposed trade, at
    every (spot_shock, vol_shock) combination in the required grid.

    `spot_by_ticker`/`sigma_by_ticker`/`t_by_ticker` supply each existing
    position's current underlying price / IV / time-to-expiry — the Risk
    Engine resolves these from `CurrentMarketData` before calling this
    function; a ticker missing from these maps cannot be stressed and
    raises `StressTestError` (fail closed, not silently skipped).
    """
    scenarios: list[PortfolioStressScenario] = []
    capital_util = capital_deployed_pct(portfolio)

    for spot_shock in STRESS_SPOT_SHOCKS:
        for vol_shock in STRESS_VOL_SHOCKS:
            total_pnl = 0.0
            delta_change = 0.0
            vega_exposure = 0.0

            for position in portfolio.positions:
                spot = spot_by_ticker.get(position.ticker)
                sigma = sigma_by_ticker.get(position.ticker)
                t = t_by_ticker.get(position.ticker)
                if spot is None or sigma is None or t is None:
                    raise StressTestError(
                        f"no current market data supplied for {position.ticker}; cannot stress-test this position"
                    )
                quant_position = _position_to_quant(position)
                [scenario] = stress_test(
                    quant_position, spot, sigma, t, rate, spot_shocks=(spot_shock,), vol_shocks=(vol_shock,)
                )
                total_pnl += scenario.pnl

                shocked_spot = spot * (1 + spot_shock)
                shocked_sigma = max(sigma * (1 + vol_shock), 1e-6)
                base_greeks = net_greeks(quant_position.legs, spot, t, rate, sigma, quant_position.underlying_shares)
                shocked_greeks = net_greeks(
                    quant_position.legs, shocked_spot, t, rate, shocked_sigma, quant_position.underlying_shares
                )
                delta_change += shocked_greeks.delta - base_greeks.delta
                vega_exposure += shocked_greeks.vega

            if new_position is not None:
                if new_position_spot is None or new_position_sigma is None or new_position_t is None:
                    raise StressTestError("new_position supplied without its spot/sigma/t")
                [scenario] = stress_test(
                    new_position,
                    new_position_spot,
                    new_position_sigma,
                    new_position_t,
                    rate,
                    spot_shocks=(spot_shock,),
                    vol_shocks=(vol_shock,),
                )
                total_pnl += scenario.pnl

            portfolio_loss_pct = max(-total_pnl, 0.0) / portfolio.nav
            scenarios.append(
                PortfolioStressScenario(
                    spot_shock_pct=spot_shock,
                    vol_shock_pct=vol_shock,
                    portfolio_pnl=total_pnl,
                    portfolio_loss_pct_of_nav=portfolio_loss_pct,
                    net_delta_change=delta_change,
                    net_vega_exposure=vega_exposure,
                    capital_utilization_pct=capital_util,
                )
            )

    worst_case = max((s.portfolio_loss_pct_of_nav for s in scenarios), default=0.0)
    return PortfolioStressResult(scenarios=scenarios, worst_case_loss_pct_of_nav=worst_case)
