"""Statistical-confidence methods for the validation report: bootstrap
confidence intervals, a forward Monte Carlo projection built by
resampling the validation run's own observed trade outcomes, VaR/CVaR
(reusing `src.backtest.metrics`), and the OBSERVED/ESTIMATED/PROJECTED
labeling discipline every number in the scorecard (`src.validation
.scorecard`) is required to carry.

**Why resampling, not a parametric model.** This platform already has a
true, GBM-based Monte Carlo engine (`src.quant.monte_carlo`) for pricing
a *single option position* under a lognormal terminal-price assumption —
reused as-is anywhere this codebase prices an option. Forward-projecting
a *portfolio of realized trade outcomes* is a different question with no
assumed distribution (a discretionary strategy's trade P&L is not
lognormal), so this module uses bootstrap resampling of the run's own
`realistic_pnl` values instead of inventing a second, parametric
portfolio model — the standard, distribution-free approach for this kind
of question, and honest about resting entirely on the trades actually
observed (see `ConfidenceLabel` below).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date
from typing import Literal

from src.backtest.metrics import historical_cvar, historical_var

ConfidenceLabel = Literal["OBSERVED", "ESTIMATED", "PROJECTED"]
"""OBSERVED: computed directly from a sample at or above the protocol's
own minimum (`config/validation.yaml`'s `minimum_completed_trades`).
ESTIMATED: computed from a real but sub-minimum sample — a genuine
number, not a guess, but statistically thin. PROJECTED: extrapolated
beyond the observed window entirely (e.g. annualizing a 90-day return,
or a Monte Carlo forward path) — never to be read as a measurement."""


@dataclass(frozen=True)
class LabeledValue:
    value: float
    label: ConfidenceLabel
    basis: str


def label_for_sample(n: int, *, minimum: int) -> ConfidenceLabel:
    if n >= minimum:
        return "OBSERVED"
    return "ESTIMATED"


def labeled_from_sample(value: float, n: int, *, minimum: int, description: str) -> LabeledValue:
    label = label_for_sample(n, minimum=minimum)
    basis = f"{description}: n={n} (minimum for OBSERVED is {minimum})"
    return LabeledValue(value=value, label=label, basis=basis)


def annualize_return(period_return: float, period_days: int) -> LabeledValue:
    """Always PROJECTED — annualizing any period shorter than a year is
    an extrapolation, never a direct measurement, regardless of how
    large the underlying trade sample was."""
    if period_days <= 0:
        raise ValueError("period_days must be positive")
    years = period_days / 365.0
    annualized = (1.0 + period_return) ** (1.0 / years) - 1.0
    return LabeledValue(
        value=annualized,
        label="PROJECTED",
        basis=f"extrapolated from a {period_days}-day observed period ({period_return:.4%}) to a 1-year figure",
    )


# ----------------------------------------------------------- bootstrap


def bootstrap_confidence_interval(
    values: list[float],
    *,
    iterations: int,
    confidence_pct: float,
    seed: int,
) -> tuple[float, float, float]:
    """Percentile bootstrap of the mean of `values`. Returns
    `(point_estimate, lower, upper)`. `values` with fewer than 2 entries
    returns a degenerate interval equal to the point estimate (or all
    zeros if empty) rather than raising — callers are expected to check
    sample size themselves via `label_for_sample` before deciding how
    much to trust the interval, not have this function silently fail."""
    if not values:
        return (0.0, 0.0, 0.0)
    point_estimate = sum(values) / len(values)
    if len(values) < 2:
        return (point_estimate, point_estimate, point_estimate)

    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(iterations):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    tail = (1.0 - confidence_pct) / 2.0
    lower_idx = max(0, min(int(tail * iterations), iterations - 1))
    upper_idx = max(0, min(int((1.0 - tail) * iterations) - 1, iterations - 1))
    return (point_estimate, means[lower_idx], means[upper_idx])


# ------------------------------------------------------ monte carlo


@dataclass(frozen=True)
class MonteCarloProjection:
    starting_nav: float
    num_future_trades: int
    iterations: int
    median_terminal_nav: float
    p5_terminal_nav: float
    p95_terminal_nav: float
    probability_of_loss: float
    basis_trade_count: int


def monte_carlo_forward_projection(
    trade_pnls: list[float],
    *,
    starting_nav: float,
    num_future_trades: int,
    iterations: int,
    seed: int,
) -> MonteCarloProjection:
    """Resamples-with-replacement from the validation run's own observed
    per-trade P&L to simulate `num_future_trades` additional trades,
    `iterations` times, and reports the resulting terminal-NAV
    distribution. Always a PROJECTED figure (see `ConfidenceLabel`) —
    it assumes the *future* trade-outcome distribution matches the
    *observed* one, which a short validation window cannot itself prove."""
    if starting_nav <= 0:
        raise ValueError("starting_nav must be positive")
    if not trade_pnls:
        return MonteCarloProjection(
            starting_nav=starting_nav, num_future_trades=num_future_trades, iterations=iterations,
            median_terminal_nav=starting_nav, p5_terminal_nav=starting_nav, p95_terminal_nav=starting_nav,
            probability_of_loss=0.0, basis_trade_count=0,
        )

    rng = random.Random(seed)
    n = len(trade_pnls)
    terminal_navs: list[float] = []
    for _ in range(iterations):
        path_nav = starting_nav
        for _ in range(num_future_trades):
            path_nav += trade_pnls[rng.randrange(n)]
        terminal_navs.append(path_nav)
    terminal_navs.sort()

    def _percentile(p: float) -> float:
        idx = max(0, min(int(p * iterations), iterations - 1))
        return terminal_navs[idx]

    median = _percentile(0.5)
    p5 = _percentile(0.05)
    p95 = _percentile(0.95)
    prob_of_loss = sum(1 for v in terminal_navs if v < starting_nav) / iterations

    return MonteCarloProjection(
        starting_nav=starting_nav, num_future_trades=num_future_trades, iterations=iterations,
        median_terminal_nav=median, p5_terminal_nav=p5, p95_terminal_nav=p95,
        probability_of_loss=prob_of_loss, basis_trade_count=n,
    )


# ------------------------------------------------------------ VaR/CVaR


def value_at_risk(equity_curve: list[tuple[date, float]], *, confidence_pct: float) -> float:
    """Thin wrapper over `src.backtest.metrics.historical_var` — never a
    second implementation of the same statistic."""
    return historical_var(equity_curve, confidence=confidence_pct)


def conditional_value_at_risk(equity_curve: list[tuple[date, float]], *, confidence_pct: float) -> float:
    return historical_cvar(equity_curve, confidence=confidence_pct)
