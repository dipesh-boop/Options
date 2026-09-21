"""The Strategy Competition Engine's final selection step: given a set
of already-priced, already-portfolio-fitted candidates for one
opportunity (each strategy generated only if `src.strategies
.suitability` allows it, only from `src.strategies.regime_mapping`'s
guideline list for the caller's stated market view), plus each
candidate's Devil's Advocate verdict and Risk Engine decision, choose
the single best risk-adjusted candidate — or NO_TRADE.

This module does not itself run the Multi-Agent Layer or the Risk
Engine (those are unchanged: `src.llm.devils_advocate`, `src.llm
.portfolio_manager`, `src.risk.engine`) — it takes their verdicts as
input, the same "Python assembles, doesn't originate" pattern every
other orchestration module in this codebase already follows
(`src.workflows.morning_scan` takes already-fetched market data;
`src.orchestration.pipeline` takes already-instantiated stages).

NO_TRADE is a first-class competitor, not a fallback: it wins whenever
no surviving candidate's risk-adjusted score clears
`no_trade_hurdle` (default 0.0 — a candidate must have a genuinely
positive risk-adjusted expected value, not merely be the "least bad"
negative-EV option).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.risk.reason_codes import RiskDecision
from src.strategies.base import StrategyEvaluation, StrategyKind
from src.strategies.comparison import ComparisonRow, build_comparison_table, rank_candidates, risk_adjusted_score
from src.strategies.portfolio_fit import PortfolioFitResult, evaluate_portfolio_fit
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)


@dataclass(frozen=True)
class CandidateVerdicts:
    """Ex-ante facts about one candidate that only exist outside
    `src.strategies` — supplied by the caller after actually running
    the Devil's Advocate and Risk Engine on it, never invented here."""

    devils_advocate_do_not_advance: bool
    devils_advocate_reason: str
    risk_decision: RiskDecision
    risk_reason: str


@dataclass(frozen=True)
class SelectionOutcome:
    selected: StrategyEvaluation | None  # None means NO_TRADE
    selection_reason: str
    comparison_table: tuple[ComparisonRow, ...]
    candidates_considered: tuple[StrategyKind, ...]
    rejected_reasons: dict[StrategyKind, str]  # every non-selected candidate's reason, incl. NO_TRADE's own if it lost


def select_best_or_no_trade(
    evaluations: list[StrategyEvaluation],
    verdicts: dict[StrategyKind, CandidateVerdicts],
    *,
    portfolio: Portfolio,
    limits: RiskLimitsConfig,
    spot: float,
    no_trade_hurdle: float = 0.0,
) -> SelectionOutcome:
    considered = tuple(ev.strategy_kind for ev in evaluations)
    rejected: dict[StrategyKind, str] = {}

    fits: dict[StrategyKind, PortfolioFitResult] = {ev.strategy_kind: evaluate_portfolio_fit(ev, portfolio, limits) for ev in evaluations}
    table = build_comparison_table(evaluations, fits, spot=spot)

    survivors: list[StrategyEvaluation] = []
    for ev in evaluations:
        v = verdicts.get(ev.strategy_kind)
        if v is None:
            rejected[ev.strategy_kind] = "no Devil's Advocate / Risk Engine verdict supplied"
            continue
        if v.devils_advocate_do_not_advance:
            rejected[ev.strategy_kind] = f"Devil's Advocate: {v.devils_advocate_reason}"
            continue
        if v.risk_decision not in _ACCEPTABLE_RISK_DECISIONS:
            rejected[ev.strategy_kind] = f"Risk Engine ({v.risk_decision.value}): {v.risk_reason}"
            continue
        fit = fits[ev.strategy_kind]
        if fit.exceeds_underlying_limit or fit.exceeds_sector_limit:
            rejected[ev.strategy_kind] = (
                f"portfolio fit: underlying exposure {fit.post_trade_underlying_exposure_pct:.2%} / "
                f"sector exposure {fit.post_trade_sector_exposure_pct:.2%} would exceed configured limits"
            )
            continue
        survivors.append(ev)

    if not survivors:
        return SelectionOutcome(
            selected=None, selection_reason="no candidate survived Devil's Advocate / Risk Engine / portfolio-fit screening",
            comparison_table=tuple(table), candidates_considered=considered, rejected_reasons=rejected,
        )

    ranked = rank_candidates(survivors)
    best = ranked[0]
    best_score = risk_adjusted_score(best)

    if best_score <= no_trade_hurdle:
        for ev in ranked:
            rejected[ev.strategy_kind] = (
                f"risk-adjusted score {risk_adjusted_score(ev):.4f} did not clear the NO_TRADE hurdle "
                f"({no_trade_hurdle:.4f}) -- cash is a valid position"
            )
        return SelectionOutcome(
            selected=None, selection_reason="no candidate's risk-adjusted expected value cleared the NO_TRADE hurdle",
            comparison_table=tuple(table), candidates_considered=considered, rejected_reasons=rejected,
        )

    for ev in ranked[1:]:
        rejected[ev.strategy_kind] = (
            f"risk_adjusted_score {risk_adjusted_score(ev):.4f} below the selected candidate's "
            f"{best_score:.4f} ({best.strategy_kind.value})"
        )

    return SelectionOutcome(
        selected=best,
        selection_reason=f"highest risk-adjusted score ({best_score:.4f}) among surviving candidates",
        comparison_table=tuple(table), candidates_considered=considered, rejected_reasons=rejected,
    )
