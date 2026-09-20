"""`run_weekly_review`/`render_weekly_review_report` — the Python engine
behind the `/weekly-review` slash command (Step 16), chaired by the
Portfolio Manager as an Investment Committee. Every numeric section
reuses already-built, already-tested machinery:
`src.workflows.performance_review` (itself built on `src.backtest
.metrics`/`benchmark` and `src.research.performance_breakdown`),
`src.workflows.decision_quality`, `src.workflows.rejected_trade_review`,
and `src.workflows.execution_quality`. This module's own job is
assembling those into one `WeeklyReviewReport` and handing identified
hypotheses to `src.research.hypothesis.HypothesisRegistry` — never
`src.research.promotion`, which this module does not import and cannot
reach.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from src.backtest.simulator import TradeRecord
from src.data.earnings import EarningsEvent, is_within_earnings_window
from src.data.historical import HistoricalBar
from src.research.hypothesis import Hypothesis, HypothesisRecord, HypothesisRegistry
from src.research.performance_breakdown import PerformanceBreakdownReport, ResearchTradeObservation
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, capital_deployed_pct, sector_exposure_pct, underlying_exposure_pct
from src.workflows.decision_quality import (
    DecisionQualityInputs,
    DecisionQualityResult,
    DecisionQualitySummary,
    classify_decision_quality,
    summarize_decision_quality,
)
from src.workflows.execution_quality import FidelitySlippageRecord, SlippageSummary, summarize_slippage
from src.workflows.performance_review import (
    PortfolioPerformanceSection,
    RiskSection,
    build_breakdowns,
    build_portfolio_performance,
    build_risk_section,
    build_trade_statistics,
)
from src.workflows.rejected_trade_review import HypotheticalOutcome, RejectedTradeStatistics, summarize_rejected_outcomes


@dataclass(frozen=True)
class NextWeekSection:
    known_economic_events: tuple[str, ...]
    earnings_risks: tuple[str, ...]
    expiration_risks: tuple[str, ...]
    portfolio_risks: tuple[str, ...]
    positions_requiring_attention: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyReviewInputs:
    now: datetime
    equity_curve: list[tuple[date, float]]
    week_start: date
    month_start: date
    year_start: date
    risk_free_annual_rate: float
    portfolio: Portfolio
    trades: list[TradeRecord]
    trade_observations: list[ResearchTradeObservation]
    decision_quality_inputs: list[DecisionQualityInputs]
    rejected_outcomes: list[HypotheticalOutcome]
    slippage_records: list[FidelitySlippageRecord]
    committee_hypotheses: list[Hypothesis]
    hypothesis_registry: HypothesisRegistry
    spy_bars: list[HistoricalBar] | None = None
    limits: RiskLimitsConfig | None = None
    known_economic_events: tuple[str, ...] = ()
    upcoming_earnings: dict[str, EarningsEvent | None] = field(default_factory=dict)
    earnings_window_days: int = 7
    net_delta: float | None = None
    net_theta: float | None = None
    net_vega: float | None = None


@dataclass(frozen=True)
class WeeklyReviewReport:
    generated_at: datetime
    performance: PortfolioPerformanceSection
    risk: RiskSection
    trade_count: int
    trade_statistics: dict[str, float]
    breakdowns: tuple[PerformanceBreakdownReport, ...]
    decision_quality: tuple[DecisionQualityResult, ...]
    decision_quality_summary: DecisionQualitySummary
    rejected_trade_statistics: RejectedTradeStatistics
    slippage_summary: SlippageSummary
    registered_hypotheses: tuple[HypothesisRecord, ...]
    next_week: NextWeekSection


def register_committee_hypotheses(hypotheses: list[Hypothesis], registry: HypothesisRegistry) -> tuple[HypothesisRecord, ...]:
    """"Send hypotheses to Strategy Research Agent" — literally
    registering them into the same `HypothesisRegistry`
    `src.llm.strategy_research` reads from, never a separate hand-off
    channel. Identifying *what* the hypotheses are is this command's own
    (Portfolio-Manager-chaired, qualitative) job; this function only
    performs the deterministic registration step."""
    return tuple(registry.register(h) for h in hypotheses)


def build_next_week_section(inputs: WeeklyReviewInputs, risk: RiskSection) -> NextWeekSection:
    earnings_risks: list[str] = []
    expiration_risks: list[str] = []
    attention: list[str] = []

    for position in inputs.portfolio.positions:
        dte = (position.expiration - inputs.now.date()).days
        if dte <= 7:
            expiration_risks.append(f"{position.ticker} {position.strategy.value} expires in {dte}d ({position.expiration.isoformat()})")
            attention.append(f"{position.ticker}: expiration within {dte}d")
        event = inputs.upcoming_earnings.get(position.ticker)
        if event is not None and is_within_earnings_window(event, position.expiration, window_days=inputs.earnings_window_days):
            earnings_risks.append(
                f"{position.ticker} earnings {event.earnings_date.isoformat()} falls within the window of its "
                f"{position.expiration.isoformat()} expiration"
            )
            attention.append(f"{position.ticker}: earnings-window exposure")

    portfolio_risks: list[str] = []
    limits = inputs.limits
    if limits is not None:
        if risk.current_drawdown_pct >= limits.drawdown_halt_pct:
            portfolio_risks.append(f"current drawdown {risk.current_drawdown_pct:.1%} is at or beyond the halt threshold ({limits.drawdown_halt_pct:.1%})")
        elif risk.current_drawdown_pct >= limits.drawdown_risk_reduction_pct:
            portfolio_risks.append(f"current drawdown {risk.current_drawdown_pct:.1%} is in the risk-reduction zone (>= {limits.drawdown_risk_reduction_pct:.1%})")
        if risk.capital_deployed_pct >= limits.normal_max_capital_deployed_pct:
            portfolio_risks.append(f"capital deployed {risk.capital_deployed_pct:.1%} is at or above the normal max ({limits.normal_max_capital_deployed_pct:.1%})")
        for sector, pct in sorted(risk.sector_exposure.items()):
            if pct >= limits.max_sector_exposure_pct:
                portfolio_risks.append(f"{sector} sector exposure {pct:.1%} is at or above the max ({limits.max_sector_exposure_pct:.1%})")
        for ticker, pct in sorted(risk.underlying_concentration.items()):
            if pct >= limits.max_underlying_exposure_pct:
                portfolio_risks.append(f"{ticker} concentration {pct:.1%} is at or above the max ({limits.max_underlying_exposure_pct:.1%})")

    return NextWeekSection(
        known_economic_events=tuple(inputs.known_economic_events),
        earnings_risks=tuple(earnings_risks),
        expiration_risks=tuple(expiration_risks),
        portfolio_risks=tuple(portfolio_risks),
        positions_requiring_attention=tuple(dict.fromkeys(attention)),
    )


def build_weekly_review_report(inputs: WeeklyReviewInputs) -> WeeklyReviewReport:
    performance = build_portfolio_performance(
        inputs.equity_curve, week_start=inputs.week_start, month_start=inputs.month_start, year_start=inputs.year_start,
        risk_free_annual_rate=inputs.risk_free_annual_rate, spy_bars=inputs.spy_bars,
    )

    current_drawdown_pct = (inputs.portfolio.peak_equity - inputs.portfolio.nav) / inputs.portfolio.peak_equity
    sectors = sorted(set(inputs.portfolio.sector_by_ticker.values()))
    sector_exposure = {s: sector_exposure_pct(inputs.portfolio, s) for s in sectors}
    underlyings = sorted({p.ticker for p in inputs.portfolio.positions})
    underlying_concentration = {t: underlying_exposure_pct(inputs.portfolio, t) for t in underlyings}

    risk = build_risk_section(
        inputs.equity_curve, risk_free_annual_rate=inputs.risk_free_annual_rate, current_drawdown_pct=current_drawdown_pct,
        cash=inputs.portfolio.cash, capital_deployed_pct=capital_deployed_pct(inputs.portfolio),
        sector_exposure=sector_exposure, underlying_concentration=underlying_concentration,
        net_delta=inputs.net_delta, net_theta=inputs.net_theta, net_vega=inputs.net_vega,
    )

    trade_stats = build_trade_statistics(inputs.trades)
    breakdowns = build_breakdowns(inputs.trade_observations)

    decision_results = tuple(classify_decision_quality(d) for d in inputs.decision_quality_inputs)
    decision_summary = summarize_decision_quality(list(decision_results))

    rejected_stats = summarize_rejected_outcomes(inputs.rejected_outcomes)
    slippage_summary = summarize_slippage(inputs.slippage_records)

    registered = register_committee_hypotheses(inputs.committee_hypotheses, inputs.hypothesis_registry)

    next_week = build_next_week_section(inputs, risk)

    return WeeklyReviewReport(
        generated_at=inputs.now,
        performance=performance,
        risk=risk,
        trade_count=len(inputs.trades),
        trade_statistics=trade_stats,
        breakdowns=breakdowns,
        decision_quality=decision_results,
        decision_quality_summary=decision_summary,
        rejected_trade_statistics=rejected_stats,
        slippage_summary=slippage_summary,
        registered_hypotheses=registered,
        next_week=next_week,
    )


_SEP = "-" * 34
_QUADRANT_LABELS = {
    "good_decision_good_outcome": "GOOD DECISION / GOOD OUTCOME",
    "good_decision_bad_outcome": "GOOD DECISION / BAD OUTCOME",
    "bad_decision_good_outcome": "BAD DECISION / GOOD OUTCOME",
    "bad_decision_bad_outcome": "BAD DECISION / BAD OUTCOME",
}


def _pct_or_dash(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "n/a"


def render_weekly_review_report(report: WeeklyReviewReport) -> str:
    lines: list[str] = []

    lines += ["PORTFOLIO PERFORMANCE", ""]
    p = report.performance
    lines += [
        f"Starting NAV: ${p.starting_nav:,.2f}",
        f"Ending NAV: ${p.ending_nav:,.2f}",
        f"Weekly return: {_pct_or_dash(p.weekly_return)}",
        f"MTD: {_pct_or_dash(p.mtd_return)}",
        f"YTD: {_pct_or_dash(p.ytd_return)}",
        f"Since inception: {p.since_inception_return:.1%}",
        f"Annualized return: {p.annualized_return:.1%}",
    ]
    if p.benchmark is not None:
        lines += [
            f"SPY benchmark: {p.benchmark.spy_total_return:.1%} (excess {p.benchmark.excess_return_vs_spy_realistic:+.1%})",
            f"Risk-free benchmark: {p.benchmark.risk_free_return:.1%} (excess {p.benchmark.excess_return_vs_risk_free_realistic:+.1%})",
        ]
    else:
        lines += ["SPY benchmark: not supplied", "Risk-free benchmark: not supplied"]

    lines += ["", _SEP, "", "RISK", ""]
    r = report.risk
    lines += [
        f"Sharpe: {r.sharpe:.2f}",
        f"Sortino: {r.sortino:.2f}",
        f"Maximum drawdown: {r.max_drawdown:.1%}",
        f"Current drawdown: {r.current_drawdown_pct:.1%}",
        f"Delta: {r.net_delta if r.net_delta is not None else 'not tracked'}",
        f"Theta: {r.net_theta if r.net_theta is not None else 'not tracked'}",
        f"Vega: {r.net_vega if r.net_vega is not None else 'not tracked'}",
        f"Cash: ${r.cash:,.2f}",
        f"Capital deployed: {r.capital_deployed_pct:.1%}",
        "Sector exposure: " + (", ".join(f"{s}={pct:.1%}" for s, pct in sorted(r.sector_exposure.items())) if r.sector_exposure else "none"),
        "Underlying concentration: " + (", ".join(f"{t}={pct:.1%}" for t, pct in sorted(r.underlying_concentration.items())) if r.underlying_concentration else "none"),
    ]

    lines += ["", _SEP, "", "TRADE STATISTICS", ""]
    ts = report.trade_statistics
    if report.trade_count == 0:
        lines += ["(no closed trades this period)"]
    else:
        lines += [
            f"Win rate: {ts['win_rate']:.1%}",
            f"Average win: ${ts['average_winner']:,.2f}",
            f"Average loss: ${ts['average_loser']:,.2f}",
            f"Profit factor: {ts['profit_factor']:.2f}" if ts["profit_factor"] != float("inf") else "Profit factor: inf (no losers this period)",
            f"Expectancy: ${ts['expectancy']:,.2f}",
        ]

    lines += ["", _SEP, "", "BREAK DOWN PERFORMANCE", ""]
    for report_dim in report.breakdowns:
        if not report_dim.buckets:
            continue
        lines += [f"By {report_dim.dimension}:"]
        for bucket in report_dim.buckets:
            lines += [f"  {bucket.bucket}: {bucket.trade_count} trade(s), win rate {bucket.win_rate:.0%}, total P&L ${bucket.total_pnl:,.2f}"]
    if not any(rd.buckets for rd in report.breakdowns):
        lines += ["(no closed trades this period)"]

    lines += ["", _SEP, "", "DECISION QUALITY", ""]
    dq = report.decision_quality_summary
    if dq.total == 0:
        lines += ["(no closed trades this period)"]
    else:
        for label in ("good_decision_good_outcome", "good_decision_bad_outcome", "bad_decision_good_outcome", "bad_decision_bad_outcome"):
            lines += [f"{_QUADRANT_LABELS[label]}: {dq.counts[label]} ({dq.fraction(label):.0%})"]

    lines += ["", _SEP, "", "REJECTED TRADE REVIEW", ""]
    rt = report.rejected_trade_statistics
    lines += [
        f"Sample size: {rt.sample_size}",
        f"Hit rate (hypothetically profitable): {rt.hit_rate:.0%}" if rt.sample_size else "Hit rate: n/a",
        f"Average hypothetical P&L: ${rt.average_hypothetical_pnl:,.2f}",
        f"Median hypothetical P&L: ${rt.median_hypothetical_pnl:,.2f}",
    ]
    if rt.warning:
        lines += [f"WARNING: {rt.warning}"]

    lines += ["", _SEP, "", "FIDELITY EXECUTION QUALITY", ""]
    sl = report.slippage_summary
    if sl.count == 0:
        lines += ["(no confirmed Fidelity fills this period)"]
    else:
        lines += [
            f"Confirmed fills: {sl.count}",
            f"Average slippage: ${sl.average_slippage:,.4f}",
            f"Median slippage: ${sl.median_slippage:,.4f}",
            "By strategy: " + ", ".join(f"{k}=${v:,.4f}" for k, v in sorted(sl.by_strategy.items())),
            "By underlying: " + ", ".join(f"{k}=${v:,.4f}" for k, v in sorted(sl.by_underlying.items())),
            "By time of day: " + ", ".join(f"{k}=${v:,.4f}" for k, v in sorted(sl.by_time_of_day.items())),
        ]

    lines += ["", _SEP, "", "RESEARCH", ""]
    if not report.registered_hypotheses:
        lines += ["(no new hypotheses identified this week)"]
    else:
        for record in report.registered_hypotheses:
            lines += [f"Sent to Strategy Research Agent: {record.hypothesis.statement} [{record.hypothesis.hypothesis_id}]"]

    lines += ["", _SEP, "", "NEXT WEEK", ""]
    nw = report.next_week
    lines += ["Known economic events: " + (", ".join(nw.known_economic_events) if nw.known_economic_events else "none")]
    lines += ["Earnings risks: " + ("; ".join(nw.earnings_risks) if nw.earnings_risks else "none")]
    lines += ["Expiration risks: " + ("; ".join(nw.expiration_risks) if nw.expiration_risks else "none")]
    lines += ["Portfolio risks: " + ("; ".join(nw.portfolio_risks) if nw.portfolio_risks else "none")]
    lines += ["Positions requiring attention: " + ("; ".join(nw.positions_requiring_attention) if nw.positions_requiring_attention else "none")]

    return "\n".join(lines)
