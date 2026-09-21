"""Hedge effectiveness (Step 19A re-ask): PROTECTIVE_PUT and
PROTECTIVE_COLLAR exist primarily to REDUCE LOSS, never to generate
standalone profit — "do NOT label a hedge unsuccessful merely because
its standalone P&L is negative." This module structurally enforces
that rule by never emitting a success/failure label at all: every
function here returns objective dollar/percentage measures (hedge
cost, drawdown avoided, tail loss avoided, CVaR reduction, portfolio
volatility reduction, upside sacrificed, net hedge benefit), leaving
any success judgment to whoever reads the report — the same
"comparison figure, never a verdict" discipline
`src.strategies.volatility_engine` already established for volatility
decisions.

Computed via a **paired** Monte Carlo comparison: the hedge's own
`Position` (shares + hedging option legs) against a synthetic
"unhedged" `Position` (the identical shares, no options at all),
simulated from the *same* terminal-price sample (same seed, via
`src.quant.monte_carlo.simulated_terminal_payoffs`) — so the two sides
are compared under identical simulated market outcomes, never two
independently-sampled, noisier Monte Carlo runs that could make a real
hedge look better or worse than it is by chance alone.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.quant.monte_carlo import Position, payoff_profile, simulated_terminal_payoffs, tail_mean_payoff
from src.strategies.base import StrategyEvaluation, StrategyKind

HEDGE_STRATEGY_KINDS = (StrategyKind.PROTECTIVE_PUT, StrategyKind.PROTECTIVE_COLLAR)

_MC_PATHS = 20_000
_MC_SEED = 20260101
_VAR_TAIL_FRACTION = 0.05


@dataclass(frozen=True)
class HedgeEffectivenessReport:
    """Every figure here is a comparison against the same portfolio
    holding the shares unhedged, under the identical simulated terminal
    prices — never a standalone-P&L judgment on the hedge by itself."""

    strategy_kind: StrategyKind
    hedge_cost: float  # dollars; positive = net debit paid, negative = the hedge was entered at a net credit
    drawdown_avoided: float  # unhedged max_loss - hedged max_loss, dollars; positive means the hedge shrank the worst case
    tail_loss_avoided: float  # hedged CVaR - unhedged CVaR, dollars; positive means the hedge's tail outcomes were less bad
    cvar_reduction_pct: float | None  # tail_loss_avoided / |unhedged CVaR|; None when the unhedged CVaR is exactly 0
    volatility_reduction_pct: float | None  # (unhedged stdev - hedged stdev) / unhedged stdev; None when unhedged stdev is 0
    upside_sacrificed: float  # dollars, >= 0; forgone gain in the best simulated outcomes (0 for a protective put, positive for a collar's sold call)
    net_hedge_benefit: float  # tail_loss_avoided - hedge_cost - upside_sacrificed -- one reasonable combined figure, not the only possible one


def _unhedged_baseline(evaluation: StrategyEvaluation) -> Position:
    if evaluation.position.underlying_shares <= 0:
        raise ValueError(
            f"{evaluation.strategy_kind.value} evaluation has no underlying shares -- "
            "hedge effectiveness requires the covered shares to compare against"
        )
    return Position(legs=[], underlying_shares=evaluation.position.underlying_shares, underlying_cost_basis=evaluation.position.underlying_cost_basis)


def evaluate_hedge_effectiveness(
    evaluation: StrategyEvaluation,
    *,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    n_paths: int = _MC_PATHS,
    seed: int = _MC_SEED,
    tail_fraction: float = _VAR_TAIL_FRACTION,
) -> HedgeEffectivenessReport:
    if evaluation.strategy_kind not in HEDGE_STRATEGY_KINDS:
        raise ValueError(
            f"{evaluation.strategy_kind.value} is not a hedge strategy -- "
            f"expected one of {[k.value for k in HEDGE_STRATEGY_KINDS]}"
        )

    unhedged = _unhedged_baseline(evaluation)
    hedged_payoffs = simulated_terminal_payoffs(evaluation.position, spot, sigma, t, rate, n_paths, seed=seed)
    unhedged_payoffs = simulated_terminal_payoffs(unhedged, spot, sigma, t, rate, n_paths, seed=seed)

    hedged_profile = payoff_profile(evaluation.position)
    unhedged_profile = payoff_profile(unhedged)
    drawdown_avoided = unhedged_profile.max_loss - hedged_profile.max_loss

    hedged_cvar = tail_mean_payoff(hedged_payoffs, tail_fraction)
    unhedged_cvar = tail_mean_payoff(unhedged_payoffs, tail_fraction)
    tail_loss_avoided = hedged_cvar - unhedged_cvar
    cvar_reduction_pct = (tail_loss_avoided / abs(unhedged_cvar)) if unhedged_cvar != 0 else None

    hedged_std = float(np.std(hedged_payoffs))
    unhedged_std = float(np.std(unhedged_payoffs))
    volatility_reduction_pct = ((unhedged_std - hedged_std) / unhedged_std) if unhedged_std > 0 else None

    # `net_credit_or_debit` is a per-share, per-contract unit price (see
    # its own docstring in src.strategies.base) -- scale to the whole
    # position's dollar cost, the same units every other figure in this
    # report (drawdown_avoided, tail_loss_avoided, upside_sacrificed)
    # already uses, via payoff_at_expiration's own x100-per-contract
    # convention.
    hedge_cost = -evaluation.net_credit_or_debit * 100 * evaluation.contracts

    # Upside sacrificed: at the best-outcome simulated paths (the same
    # paths, by index, in both arrays -- same seed, position-independent
    # terminal-price draw), how much payoff did the hedge give up
    # relative to staying unhedged, net of the hedge's own premium cost
    # (already counted separately as `hedge_cost` above, and otherwise
    # present in *every* scenario including these best-case ones, which
    # would double-count it here). A protective put's own long put never
    # caps upside (there is no short call leg) -- once its constant
    # premium cost is subtracted out, this correctly nets to 0 for it;
    # a collar's sold call genuinely caps gains, so this stays positive
    # for a collar whenever the simulated top tail runs above that
    # call's strike.
    top_count = max(1, int(len(unhedged_payoffs) * tail_fraction))
    top_paths = np.argsort(unhedged_payoffs)[-top_count:]
    raw_top_tail_gap = float(np.mean(unhedged_payoffs[top_paths] - hedged_payoffs[top_paths]))
    upside_sacrificed = max(raw_top_tail_gap - hedge_cost, 0.0)

    net_hedge_benefit = tail_loss_avoided - hedge_cost - upside_sacrificed

    return HedgeEffectivenessReport(
        strategy_kind=evaluation.strategy_kind,
        hedge_cost=hedge_cost,
        drawdown_avoided=drawdown_avoided,
        tail_loss_avoided=tail_loss_avoided,
        cvar_reduction_pct=cvar_reduction_pct,
        volatility_reduction_pct=volatility_reduction_pct,
        upside_sacrificed=upside_sacrificed,
        net_hedge_benefit=net_hedge_benefit,
    )


MIN_SAMPLE_SIZE_FOR_HEDGE_CONCLUSIONS = 20  # same "20" convention src.validation.counterfactual/strategy_attribution already established


@dataclass(frozen=True)
class AggregateHedgeEffectiveness:
    """The same seven objective measures, averaged across every
    recorded `HedgeEffectivenessReport` for one hedge strategy -- still
    never a success/failure label, structurally: nothing here is a
    boolean or a verdict field, only averaged dollar/percentage
    figures. `meaningful_sample` gates whether those averages should be
    trusted, the same discipline `src.validation.counterfactual`
    already applies to selection-effectiveness conclusions."""

    strategy_kind: StrategyKind
    sample_size: int
    meaningful_sample: bool
    avg_hedge_cost: float
    avg_drawdown_avoided: float
    avg_tail_loss_avoided: float
    avg_cvar_reduction_pct: float | None
    avg_volatility_reduction_pct: float | None
    avg_upside_sacrificed: float
    avg_net_hedge_benefit: float


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate_hedge_effectiveness(
    reports: list[HedgeEffectivenessReport],
    *,
    strategy_kind: StrategyKind,
    min_sample_size: int = MIN_SAMPLE_SIZE_FOR_HEDGE_CONCLUSIONS,
) -> AggregateHedgeEffectiveness:
    relevant = [r for r in reports if r.strategy_kind == strategy_kind]
    cvar_vals = [r.cvar_reduction_pct for r in relevant if r.cvar_reduction_pct is not None]
    vol_vals = [r.volatility_reduction_pct for r in relevant if r.volatility_reduction_pct is not None]
    return AggregateHedgeEffectiveness(
        strategy_kind=strategy_kind,
        sample_size=len(relevant),
        meaningful_sample=len(relevant) >= min_sample_size,
        avg_hedge_cost=_mean([r.hedge_cost for r in relevant]),
        avg_drawdown_avoided=_mean([r.drawdown_avoided for r in relevant]),
        avg_tail_loss_avoided=_mean([r.tail_loss_avoided for r in relevant]),
        avg_cvar_reduction_pct=_mean(cvar_vals) if cvar_vals else None,
        avg_volatility_reduction_pct=_mean(vol_vals) if vol_vals else None,
        avg_upside_sacrificed=_mean([r.upside_sacrificed for r in relevant]),
        avg_net_hedge_benefit=_mean([r.net_hedge_benefit for r in relevant]),
    )


def build_hedge_effectiveness_report(
    reports: list[HedgeEffectivenessReport], *, min_sample_size: int = MIN_SAMPLE_SIZE_FOR_HEDGE_CONCLUSIONS
) -> dict[str, AggregateHedgeEffectiveness]:
    """Protective puts and protective collars, always reported
    separately from each other (and, by construction -- this function
    only ever sees hedge-kind reports -- separately from every
    profit-seeking strategy)."""
    return {
        kind.value: aggregate_hedge_effectiveness(reports, strategy_kind=kind, min_sample_size=min_sample_size)
        for kind in HEDGE_STRATEGY_KINDS
    }


def render_hedge_effectiveness_report(report: dict[str, AggregateHedgeEffectiveness]) -> str:
    lines: list[str] = [
        "HEDGE EFFECTIVENESS REPORT", "",
        "Protective puts and protective collars, evaluated separately from profit-seeking",
        "strategies -- never labeled unsuccessful merely because standalone P&L is negative.", "",
    ]
    for name in sorted(report):
        agg = report[name]
        lines += [name.upper(), "-" * len(name)]
        if not agg.meaningful_sample:
            lines += [f"  INSUFFICIENT SAMPLE ({agg.sample_size} evaluation(s), need {MIN_SAMPLE_SIZE_FOR_HEDGE_CONCLUSIONS})"]
        lines += [
            f"  Average hedge cost: ${agg.avg_hedge_cost:,.2f}",
            f"  Average drawdown avoided: ${agg.avg_drawdown_avoided:,.2f}",
            f"  Average tail loss avoided: ${agg.avg_tail_loss_avoided:,.2f}",
            f"  Average CVaR reduction: {agg.avg_cvar_reduction_pct:.1%}" if agg.avg_cvar_reduction_pct is not None else "  Average CVaR reduction: n/a",
            f"  Average portfolio volatility reduction: {agg.avg_volatility_reduction_pct:.1%}" if agg.avg_volatility_reduction_pct is not None else "  Average portfolio volatility reduction: n/a",
            f"  Average upside sacrificed: ${agg.avg_upside_sacrificed:,.2f}",
            f"  Average net hedge benefit: ${agg.avg_net_hedge_benefit:,.2f}",
            "",
        ]
    return "\n".join(lines)
