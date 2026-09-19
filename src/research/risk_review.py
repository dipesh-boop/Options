"""Strategy-level risk review — the "RISK COMPARISON" stage of Step
14's pipeline and the deterministic "Risk Engine review" `src.research
.promotion.promote_strategy` requires before a strategy version can be
promoted.

This is a distinct capability from `src.risk.engine.evaluate_trade_proposal`
(which gates one live/paper trade against portfolio state) — there is no
portfolio or single trade here, only a candidate strategy's own backtest
performance — but it reuses the same limits file
(`src.risk.limits.RiskLimitsConfig`) rather than inventing a second set
of thresholds, so "drawdown_halt_pct means the same thing everywhere in
this codebase" stays true.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.backtest.metrics import PerformanceMetrics
from src.risk.limits import RiskLimitsConfig, get_default_limits

StrategyRiskVerdict = Literal["PASS", "CONCERN", "REJECT"]

MIN_TRADES_FOR_CONFIDENT_RISK_READ = 30

_VERDICT_SEVERITY: dict[StrategyRiskVerdict, int] = {"PASS": 0, "CONCERN": 1, "REJECT": 2}


@dataclass(frozen=True)
class StrategyRiskReviewInputs:
    realistic_metrics: PerformanceMetrics
    theoretical_metrics: PerformanceMetrics
    trade_count: int


@dataclass(frozen=True)
class StrategyRiskReview:
    verdict: StrategyRiskVerdict
    reasons: tuple[str, ...]
    max_drawdown_pct: float
    var_95: float


def _worse(a: StrategyRiskVerdict, b: StrategyRiskVerdict) -> StrategyRiskVerdict:
    return a if _VERDICT_SEVERITY[a] >= _VERDICT_SEVERITY[b] else b


def review_strategy_risk(inputs: StrategyRiskReviewInputs, limits: RiskLimitsConfig | None = None) -> StrategyRiskReview:
    """Grades the **realistic** (never theoretical) backtest metrics
    against this platform's own portfolio-level drawdown/tail-loss
    limits, applied here at the single-strategy level as an early
    warning before a strategy is ever run against a real portfolio. A
    strategy whose own backtest already breaches a portfolio-level halt
    threshold has no business being promoted regardless of its headline
    CAGR."""
    limits = limits or get_default_limits()
    metrics = inputs.realistic_metrics

    verdict: StrategyRiskVerdict = "PASS"
    reasons: list[str] = []

    if metrics.max_drawdown >= limits.drawdown_halt_pct:
        verdict = _worse(verdict, "REJECT")
        reasons.append(
            f"realistic max drawdown {metrics.max_drawdown:.1%} meets or exceeds the portfolio "
            f"halt threshold ({limits.drawdown_halt_pct:.1%})"
        )
    elif metrics.max_drawdown >= limits.drawdown_risk_reduction_pct:
        verdict = _worse(verdict, "CONCERN")
        reasons.append(
            f"realistic max drawdown {metrics.max_drawdown:.1%} is in the risk-reduction zone "
            f"(>= {limits.drawdown_risk_reduction_pct:.1%})"
        )

    if metrics.var_95 >= limits.max_stress_loss_pct_of_nav:
        verdict = _worse(verdict, "REJECT")
        reasons.append(
            f"realistic 95% VaR {metrics.var_95:.1%} meets or exceeds the max acceptable stress "
            f"loss ({limits.max_stress_loss_pct_of_nav:.1%})"
        )

    if inputs.trade_count < MIN_TRADES_FOR_CONFIDENT_RISK_READ:
        verdict = _worse(verdict, "CONCERN")
        reasons.append(
            f"only {inputs.trade_count} trade(s) back this review, below the "
            f"{MIN_TRADES_FOR_CONFIDENT_RISK_READ}-trade threshold for a confident risk read"
        )

    return StrategyRiskReview(verdict=verdict, reasons=tuple(reasons), max_drawdown_pct=metrics.max_drawdown, var_95=metrics.var_95)
