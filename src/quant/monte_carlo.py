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


# --------------------------------------------- Step 19A: payoff profile --


@dataclass(frozen=True)
class PayoffProfile:
    """Exact at-expiration payoff shape for any `Position` (any number
    of legs, any quantity ratio, with or without underlying shares) —
    the generic engine every `src.strategies.*` module builds on,
    instead of each strategy re-deriving its own max-profit/max-loss/
    breakeven formula by hand.

    Exact, not approximate: `payoff_at_expiration` is piecewise LINEAR
    in the terminal price, with kinks only at the position's own option
    strikes (a fixed, finite set) and two unbounded rays beyond the
    lowest and highest strike. A piecewise-linear function's extrema can
    only occur at a kink or on one of the two unbounded rays — so
    evaluating payoff at every strike, at zero, and at the two rays'
    slopes is an EXACT computation, not a grid-search approximation.
    Breakevens (where payoff crosses zero) are found the same way: exact
    linear interpolation within whichever segment actually crosses zero,
    not a numeric root-finder.
    """

    max_profit: float  # math.inf if the upside ray has positive slope
    max_loss: float  # math.inf only if a disallowed/naked structure has negative downside slope
    breakeven_points: tuple[float, ...]  # sorted ascending; may be empty (never profitable) or have >2 entries
    upside_unbounded: bool
    downside_unbounded: bool


def _upside_tail_slope(position: Position) -> float:
    """Per-$1 slope of payoff as terminal price -> +infinity: each call
    leg contributes its full signed notional (a long call's slope is
    +100*quantity; a short call's is -100*quantity); puts contribute
    nothing this far OTM; each underlying share contributes +1."""
    slope = float(position.underlying_shares)
    for leg in position.legs:
        if leg.right == OptionRight.CALL:
            sign = 1 if leg.side == Side.BUY else -1
            slope += sign * 100 * leg.quantity
    return slope


def payoff_profile(position: Position) -> PayoffProfile:
    if not position.legs and position.underlying_shares == 0:
        raise ValueError("position has no legs and no underlying shares -- nothing to profile")

    strikes = sorted({leg.strike for leg in position.legs})
    candidate_prices = [0.0, *strikes]
    candidate_payoffs = [payoff_at_expiration(position, p) for p in candidate_prices]

    tail_slope = _upside_tail_slope(position)
    upside_unbounded_profit = tail_slope > 0
    upside_unbounded_loss = tail_slope < 0
    # The payoff value anywhere beyond the highest strike (or at 0 if
    # there are no strikes at all, e.g. shares alone) -- a flat plateau
    # when tail_slope == 0.
    tail_reference_price = (strikes[-1] + 1.0) if strikes else 1.0
    tail_payoff = payoff_at_expiration(position, tail_reference_price)

    all_payoffs = list(candidate_payoffs)
    if not upside_unbounded_profit and not upside_unbounded_loss:
        all_payoffs.append(tail_payoff)

    if upside_unbounded_profit:
        max_profit = math.inf
    else:
        max_profit = max(all_payoffs) if not upside_unbounded_loss else max(candidate_payoffs)
    if upside_unbounded_loss:
        max_loss = math.inf
    else:
        max_loss = -min(all_payoffs) if not upside_unbounded_profit else -min(candidate_payoffs)
    max_loss = max(max_loss, 0.0) if math.isfinite(max_loss) else max_loss
    max_profit = max(max_profit, 0.0) if math.isfinite(max_profit) else max_profit

    breakevens = _find_breakevens(position, candidate_prices, candidate_payoffs, tail_slope, tail_reference_price, tail_payoff)

    return PayoffProfile(
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_points=tuple(breakevens),
        upside_unbounded=upside_unbounded_profit,
        downside_unbounded=upside_unbounded_loss,
    )


def _find_breakevens(
    position: Position,
    candidate_prices: list[float],
    candidate_payoffs: list[float],
    tail_slope: float,
    tail_reference_price: float,
    tail_payoff: float,
) -> list[float]:
    """Exact linear interpolation for every zero-crossing segment,
    including the unbounded upside ray (if it has nonzero slope)."""
    points = list(zip(candidate_prices, candidate_payoffs))
    breakevens: list[float] = []

    for (p0, y0), (p1, y1) in zip(points, points[1:]):
        if y0 == 0.0:
            breakevens.append(p0)
        if (y0 < 0.0 < y1) or (y1 < 0.0 < y0):
            # exact root of the line segment between (p0,y0) and (p1,y1)
            root = p0 + (0.0 - y0) * (p1 - p0) / (y1 - y0)
            breakevens.append(root)

    if points:
        last_p, last_y = points[-1]
        if last_y == 0.0 and last_p not in breakevens:
            breakevens.append(last_p)

    if tail_slope != 0.0:
        # The upside ray starts at (tail_reference_price, tail_payoff)
        # with constant slope `tail_slope` per $1 -- solve for where it
        # crosses zero, if that root is actually on this ray (at or
        # beyond tail_reference_price).
        root = tail_reference_price - tail_payoff / tail_slope
        if root >= tail_reference_price - 1e-9:
            breakevens.append(root)
    elif tail_payoff == 0.0:
        breakevens.append(tail_reference_price)

    # De-duplicate (within float tolerance) and sort.
    breakevens.sort()
    deduped: list[float] = []
    for b in breakevens:
        if not deduped or abs(b - deduped[-1]) > 1e-6:
            deduped.append(b)
    return deduped
