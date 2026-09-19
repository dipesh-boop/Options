"""Monte Carlo simulation and deterministic stress testing.

Two distinct tools live here:

- True Monte Carlo (`simulate_terminal_prices`, `monte_carlo_pop_and_ev`):
  random sampling of terminal underlying prices under risk-neutral GBM,
  used to independently cross-check the closed-form probability/expected
  value estimates in src.quant.probability / src.quant.expected_value
  (ARCHITECTURE.md §6) against the full simulated payoff distribution,
  rather than the binary max-profit/max-loss approximation those modules
  use.
- A deterministic stress grid (`stress_test`): exact Black-Scholes
  repricing at fixed underlying/volatility shocks — no randomness. This
  is what "stress underlying at -20%/-10%/-5%/+5%/+10%/+20%" calls for;
  it's grouped in this module because both tools answer the same
  question ("what happens to this position under an adverse scenario"),
  just by different methods.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from src.quant.black_scholes import Leg, OptionRight, Side, price

STANDARD_SPOT_SHOCKS: tuple[float, ...] = (-0.20, -0.10, -0.05, 0.05, 0.10, 0.20)
STANDARD_VOL_SHOCKS: tuple[float, ...] = (-0.30, -0.15, 0.0, 0.15, 0.30)


@dataclass(frozen=True)
class Position:
    """An option position (one or more legs) optionally paired with an
    underlying share position, e.g. the 100 shares under a covered call.
    `underlying_shares=0` for cash-secured put / put credit spread."""

    legs: list[Leg]
    underlying_shares: int = 0
    underlying_cost_basis: float = 0.0


def net_entry_credit(position: Position) -> float:
    """Net cash received (positive) or paid (negative) at entry, per
    share, across all option legs. Excludes the underlying stock's cost
    basis, which is a separate cash flow."""
    total = 0.0
    for leg in position.legs:
        sign = 1 if leg.side == Side.SELL else -1
        total += sign * leg.entry_price * leg.quantity
    return total


def payoff_at_expiration(position: Position, terminal_price: float) -> float:
    """Total position P&L in dollars (already x100 for option legs) if
    the underlying settles at `terminal_price` at expiration."""
    total = 0.0
    for leg in position.legs:
        if leg.right == OptionRight.CALL:
            intrinsic = max(terminal_price - leg.strike, 0.0)
        else:
            intrinsic = max(leg.strike - terminal_price, 0.0)
        sign = 1 if leg.side == Side.BUY else -1
        total += sign * (intrinsic - leg.entry_price) * 100 * leg.quantity
    total += position.underlying_shares * (terminal_price - position.underlying_cost_basis)
    return total


def simulate_terminal_prices(
    spot: float, sigma: float, t: float, rate: float, n_paths: int, *, seed: int | None = None
) -> np.ndarray:
    """Risk-neutral GBM terminal prices at time t:
    S_T = S0 * exp((r - 0.5*sigma^2)*t + sigma*sqrt(t)*Z), Z ~ N(0,1).

    Risk-neutral drift is used for consistency with the Black-Scholes
    pricing measure this whole package is built on — this is a
    pricing/probability cross-check tool, not a physical-measure return
    forecast."""
    if n_paths <= 0:
        raise ValueError("n_paths must be positive")
    if t <= 0:
        return np.full(n_paths, spot, dtype=float)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n_paths)
    drift = (rate - 0.5 * sigma**2) * t
    diffusion = sigma * math.sqrt(t) * z
    return spot * np.exp(drift + diffusion)


@dataclass(frozen=True)
class MonteCarloResult:
    n_paths: int
    probability_of_profit: float
    expected_value: float
    standard_error: float  # of the expected_value estimate


def monte_carlo_pop_and_ev(
    position: Position, spot: float, sigma: float, t: float, rate: float, n_paths: int, *, seed: int | None = None
) -> MonteCarloResult:
    """Empirical probability of profit / expected value from simulated
    terminal prices — an independent cross-check against the closed-form
    estimates elsewhere in src.quant, since it integrates the actual
    (non-binary) payoff curve rather than assuming a max-profit/max-loss
    binary outcome."""
    terminal_prices = simulate_terminal_prices(spot, sigma, t, rate, n_paths, seed=seed)
    payoffs = np.array([payoff_at_expiration(position, float(s)) for s in terminal_prices])
    pop = float(np.mean(payoffs > 0))
    ev = float(np.mean(payoffs))
    se = float(np.std(payoffs, ddof=1) / math.sqrt(n_paths))
    return MonteCarloResult(n_paths=n_paths, probability_of_profit=pop, expected_value=ev, standard_error=se)


@dataclass(frozen=True)
class StressScenario:
    spot_shock_pct: float
    vol_shock_pct: float
    shocked_spot: float
    shocked_sigma: float
    pnl: float


def stress_test(
    position: Position,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    *,
    spot_shocks: tuple[float, ...] = STANDARD_SPOT_SHOCKS,
    vol_shocks: tuple[float, ...] = (0.0,),
) -> list[StressScenario]:
    """Deterministic mark-to-market P&L at each (spot_shock, vol_shock)
    combination, holding time to expiration fixed — an instantaneous
    scenario (today's shock), not a rolled-forward one.

    `vol_shocks` defaults to no vol shock (spot-only stress, the six
    required scenarios); pass `STANDARD_VOL_SHOCKS` for the full
    spot x vol grid ("stress implied volatility")."""
    scenarios: list[StressScenario] = []
    for spot_shock in spot_shocks:
        shocked_spot = spot * (1 + spot_shock)
        for vol_shock in vol_shocks:
            shocked_sigma = max(sigma * (1 + vol_shock), 1e-6)
            pnl = 0.0
            for leg in position.legs:
                theo = price(shocked_spot, leg.strike, t, rate, shocked_sigma, leg.right)
                sign = 1 if leg.side == Side.BUY else -1
                pnl += sign * (theo - leg.entry_price) * 100 * leg.quantity
            pnl += position.underlying_shares * (shocked_spot - position.underlying_cost_basis)
            scenarios.append(
                StressScenario(
                    spot_shock_pct=spot_shock,
                    vol_shock_pct=vol_shock,
                    shocked_spot=shocked_spot,
                    shocked_sigma=shocked_sigma,
                    pnl=pnl,
                )
            )
    return scenarios
