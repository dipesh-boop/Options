"""`run_morning_scan` — the Python engine behind the `/morning-scan`
slash command (Step 15). Stages 1-13 are implemented here and in
`src.workflows.feed_health`/`reconciliation`/`candidate_generation`;
stages 14-21 are `src.orchestration.pipeline.run_order_pipeline`,
called once per generated candidate, completely unchanged — this module
never reimplements Quant Engine, Devil's Advocate, Portfolio Manager,
Risk Engine (whose own `src.risk.correlation`/`src.risk.concentration`
checks are stages 17-18), PaperBroker, or Fidelity ticket generation.

**This module never places a Fidelity order and never can.** Every
`FidelityTradeTicket` it surfaces was produced by
`run_order_pipeline`'s own Risk-Engine-driven MANUAL-capability call
(`src.brokers.fidelity.FidelityManualProvider`, the only thing in this
codebase that builds one) and defaults to `AWAITING_HUMAN` by
construction (`FidelityTradeTicket.status`'s own default) — there is no
function anywhere in this module, or imported by it, that could move a
ticket past that on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from src.brokers.fidelity import FidelityTradeTicket, render_ticket_text
from src.data.earnings import EarningsEvent, is_within_earnings_window
from src.data.option_chain import OptionChain
from src.llm.context import MarketSnapshotContext
from src.llm.schemas import MarketRegimeAssessment, RiskReviewNote, StrategyType, TradeProposal
from src.orchestration.pipeline import PipelineRequest, PipelineStages, PipelineStatus, run_order_pipeline
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct
from src.risk.reason_codes import RiskDecision
from src.workflows.candidate_generation import Candidate, QuantFilterConfig, UniverseEntry, generate_candidates
from src.workflows.feed_health import FeedHealthReport, FreshnessReport, verify_data_freshness, verify_market_data_feeds
from src.workflows.reconciliation import ConfirmedFidelityPosition, ReconciliationResult, reconcile_portfolio

DEFAULT_MAX_CANDIDATES = 5
DEFAULT_EARNINGS_WINDOW_DAYS = 7


@dataclass(frozen=True)
class MorningScanInputs:
    now: datetime
    portfolio: Portfolio
    limits: RiskLimitsConfig
    confirmed_fidelity_positions: list[ConfirmedFidelityPosition]
    fetch_results: dict[str, OptionChain | Exception]
    universe: list[UniverseEntry]
    strategies: list[StrategyType]
    quant_filter: QuantFilterConfig
    market_regime: MarketRegimeAssessment
    risk_reviewer_note_factory: Callable[[TradeProposal], RiskReviewNote]
    snapshot_factory: Callable[[OptionChain, Candidate], MarketSnapshotContext]
    stages: PipelineStages
    automated_broker_capabilities: BrokerCapabilities | None
    manual_broker_capabilities: BrokerCapabilities | None
    vix_level: float | None = None
    notable_economic_events: tuple[str, ...] = ()
    earnings_by_ticker: dict[str, EarningsEvent | None] | None = None
    earnings_window_days: int = DEFAULT_EARNINGS_WINDOW_DAYS
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    portfolio_net_delta: float | None = None
    portfolio_net_theta: float | None = None
    portfolio_net_vega: float | None = None


@dataclass(frozen=True)
class CandidateResult:
    candidate: Candidate
    outcome: object  # src.orchestration.pipeline.PipelineOutcome; typed loosely to avoid a circular annotation import at module load


@dataclass(frozen=True)
class MorningScanReport:
    generated_at: datetime
    feed_health: FeedHealthReport
    freshness: FreshnessReport
    reconciliation: ReconciliationResult
    nav: float
    cash: float
    capital_deployed_pct: float
    cash_reserve_pct: float
    current_drawdown_pct: float
    sector_exposure: dict[str, float]
    portfolio_net_delta: float | None
    portfolio_net_theta: float | None
    portfolio_net_vega: float | None
    market_regime: MarketRegimeAssessment
    vix_level: float | None
    notable_economic_events: tuple[str, ...]
    earnings_screened_out: tuple[str, ...]
    reconciliation_screened_out: tuple[str, ...]
    results: tuple[CandidateResult, ...]
    no_trade_reason: str | None

    @property
    def any_actionable(self) -> bool:
        return any(r.outcome.fidelity_ticket is not None for r in self.results)


def _screen_out_earnings_window(
    candidates: list[Candidate], event: EarningsEvent | None, window_days: int
) -> tuple[list[Candidate], list[str]]:
    if event is None:
        return candidates, []
    kept: list[Candidate] = []
    screened_out: list[str] = []
    for c in candidates:
        if is_within_earnings_window(event, c.proposal.expiration, window_days=window_days):
            screened_out.append(f"{c.proposal.ticker} {c.proposal.strategy.value} {c.proposal.expiration.isoformat()} (earnings window)")
        else:
            kept.append(c)
    return kept, screened_out


async def run_morning_scan(inputs: MorningScanInputs) -> MorningScanReport:
    feed_health = verify_market_data_feeds(inputs.fetch_results)
    fresh_chains: dict[str, OptionChain] = {t: c for t, c in inputs.fetch_results.items() if not isinstance(c, Exception)}
    freshness = verify_data_freshness(fresh_chains, inputs.now)
    reconciliation = reconcile_portfolio(inputs.confirmed_fidelity_positions, inputs.portfolio)

    current_drawdown_pct = (inputs.portfolio.peak_equity - inputs.portfolio.nav) / inputs.portfolio.peak_equity
    sectors = sorted(set(inputs.portfolio.sector_by_ticker.values()))
    sector_exposure = {sector: sector_exposure_pct(inputs.portfolio, sector) for sector in sectors}

    # SY-006 fix: a ticker reconciliation has already flagged as
    # "confirmed in Fidelity but not tracked internally" must not also
    # get a fresh candidate generated for it in the same run -- without
    # this, the very report that flags the discrepancy could still
    # produce (and potentially get approved) a real duplicate trade
    # recommendation for a position that's already open, simply because
    # the duplicate-position check only ever sees `portfolio.positions`
    # (the same stale internal view the discrepancy is about), never
    # the confirmed-but-untracked Fidelity position itself.
    tickers_with_untracked_fidelity_positions = {
        d.ticker for d in reconciliation.discrepancies if d.kind == "missing_from_internal"
    }

    earnings_by_ticker = inputs.earnings_by_ticker or {}
    earnings_screened_out: list[str] = []
    reconciliation_screened_out: list[str] = []
    candidate_pool: list[Candidate] = []
    for entry in inputs.universe:
        if entry.ticker in tickers_with_untracked_fidelity_positions:
            reconciliation_screened_out.append(
                f"{entry.ticker}: confirmed Fidelity position not yet tracked internally (see reconciliation)"
            )
            continue
        chain = fresh_chains.get(entry.ticker)
        if chain is None:
            continue
        raw_candidates = generate_candidates(
            entry, chain, inputs.strategies, inputs.quant_filter, inputs.limits, inputs.portfolio,
            inputs.market_regime.regime, now=inputs.now,
        )
        kept, screened_out = _screen_out_earnings_window(raw_candidates, earnings_by_ticker.get(entry.ticker), inputs.earnings_window_days)
        earnings_screened_out.extend(screened_out)
        candidate_pool.extend(kept)

    # Richest stated credit first -- a simple, provisional ranking
    # heuristic pending a real Opportunity Scanner (still an open gap;
    # see progress.md). This never changes *whether* a candidate is
    # eligible, only the order the (bounded) full pipeline evaluates
    # them in.
    candidate_pool.sort(key=lambda c: c.proposal.target_entry, reverse=True)
    candidate_pool = candidate_pool[: inputs.max_candidates]

    results: list[CandidateResult] = []
    # SY-004 fix: start from the pre-scan snapshot, then thread each
    # candidate's own `updated_portfolio` forward as the *next*
    # candidate's Risk Engine input. Without this, every candidate in
    # the batch is checked against the same stale, pre-scan cash/
    # position state, so the Risk Engine's cash-reserve/capital-deployed/
    # concentration/correlation/duplicate-position checks never see what
    # this same run has already approved moments earlier -- two
    # individually-compliant candidates could together breach a limit
    # that applies to the portfolio as a whole. A candidate that didn't
    # fill (rejected, no fill, reprice required) leaves `current_portfolio`
    # unchanged, since there is nothing new for the next candidate to see.
    current_portfolio = inputs.portfolio
    for candidate in candidate_pool:
        chain = fresh_chains[candidate.proposal.ticker]
        snapshot = inputs.snapshot_factory(chain, candidate)
        request = PipelineRequest(
            proposal=candidate.proposal,
            market_data=chain,
            portfolio=current_portfolio,
            limits=inputs.limits,
            market_regime=inputs.market_regime,
            risk_reviewer_note=inputs.risk_reviewer_note_factory(candidate.proposal),
            analysis_snapshot=snapshot,
            current_snapshot=snapshot,
            automated_broker_capabilities=inputs.automated_broker_capabilities,
            manual_broker_capabilities=inputs.manual_broker_capabilities,
            now=inputs.now,
        )
        outcome = await run_order_pipeline(request, inputs.stages)
        results.append(CandidateResult(candidate=candidate, outcome=outcome))
        if outcome.updated_portfolio is not None:
            current_portfolio = outcome.updated_portfolio

    no_trade_reason = _no_trade_reason(results, candidate_pool)

    return MorningScanReport(
        generated_at=inputs.now,
        feed_health=feed_health,
        freshness=freshness,
        reconciliation=reconciliation,
        nav=inputs.portfolio.nav,
        cash=inputs.portfolio.cash,
        capital_deployed_pct=(inputs.portfolio.nav - inputs.portfolio.cash) / inputs.portfolio.nav,
        cash_reserve_pct=inputs.portfolio.cash / inputs.portfolio.nav,
        current_drawdown_pct=current_drawdown_pct,
        sector_exposure=sector_exposure,
        portfolio_net_delta=inputs.portfolio_net_delta,
        portfolio_net_theta=inputs.portfolio_net_theta,
        portfolio_net_vega=inputs.portfolio_net_vega,
        market_regime=inputs.market_regime,
        vix_level=inputs.vix_level,
        notable_economic_events=tuple(inputs.notable_economic_events),
        earnings_screened_out=tuple(earnings_screened_out),
        reconciliation_screened_out=tuple(reconciliation_screened_out),
        results=tuple(results),
        no_trade_reason=no_trade_reason,
    )


def _no_trade_reason(results: list[CandidateResult], candidate_pool: list[Candidate]) -> str | None:
    """Never fabricates urgency: "cash is a valid position" is the
    default outcome whenever nothing has actually cleared every gate,
    not a fallback to reach for only when everything else looks bad."""
    if any(r.outcome.fidelity_ticket is not None for r in results):
        return None
    if not candidate_pool:
        return (
            "No candidates cleared universe/liquidity/quant screening this session (or every "
            "eligible ticker's market data was stale or unavailable). Cash is a valid position."
        )
    reasons = "; ".join(f"{r.candidate.proposal.ticker} {r.candidate.proposal.strategy.value}: {r.outcome.reason}" for r in results)
    return f"No candidate reached risk approval. {reasons}. Cash is a valid position."


_SEP = "-" * 34


def render_morning_scan_report(report: MorningScanReport) -> str:
    """The exact section order Step 15 specifies: MARKET REGIME,
    PORTFOLIO, TOP OPPORTUNITIES, DEVIL'S ADVOCATE, RISK ENGINE,
    FIDELITY TRADE TICKET (or NO TRADE)."""
    lines: list[str] = []

    lines += ["MARKET REGIME", ""]
    lines += [f"Regime: {report.market_regime.regime}"]
    lines += [f"Commentary: {report.market_regime.commentary}"]
    if report.vix_level is not None:
        lines += [f"VIX: {report.vix_level:.2f}"]
    lines += [f"Notable events: {', '.join(report.notable_economic_events) if report.notable_economic_events else 'none'}"]
    if report.market_regime.notable_events:
        lines += [f"Regime-flagged events: {', '.join(report.market_regime.notable_events)}"]
    if not report.feed_health.all_healthy:
        lines += [f"Feed issues: {', '.join(report.feed_health.unhealthy_symbols)}"]
    if not report.freshness.all_fresh:
        lines += [f"Stale data: {', '.join(report.freshness.stale_symbols)}"]
    if not report.reconciliation.clean:
        lines += [f"Reconciliation discrepancies: {len(report.reconciliation.discrepancies)} (see log)"]
    if report.reconciliation_screened_out:
        lines += [f"Screened out pending reconciliation: {'; '.join(report.reconciliation_screened_out)}"]
    if report.earnings_screened_out:
        lines += [f"Screened out for earnings window: {'; '.join(report.earnings_screened_out)}"]

    lines += ["", _SEP, "", "PORTFOLIO", ""]
    lines += [
        f"NAV: ${report.nav:,.2f}",
        f"Cash: ${report.cash:,.2f}",
        f"Capital deployed: {report.capital_deployed_pct:.1%}",
        f"Current drawdown: {report.current_drawdown_pct:.1%}",
        f"Delta: {report.portfolio_net_delta if report.portfolio_net_delta is not None else 'not tracked'}",
        f"Theta: {report.portfolio_net_theta if report.portfolio_net_theta is not None else 'not tracked'}",
        f"Vega: {report.portfolio_net_vega if report.portfolio_net_vega is not None else 'not tracked'}",
        "Sector concentration: " + (", ".join(f"{s}={pct:.1%}" for s, pct in sorted(report.sector_exposure.items())) if report.sector_exposure else "none"),
    ]

    lines += ["", _SEP, "", "TOP OPPORTUNITIES", ""]
    if not report.results:
        lines += ["(none passed universe/liquidity/quant screening)"]
    for r in report.results:
        p = r.candidate.proposal
        qa = r.outcome.quantitative_analysis
        lines += [
            f"Ticker: {p.ticker}",
            f"Strategy: {p.strategy.value}",
            f"Expiration: {p.expiration.isoformat()} (DTE {(p.expiration - report.generated_at.date()).days})",
            f"Strikes: {', '.join(f'{leg.side.value} {leg.right.value} {leg.strike:g}' for leg in p.legs)}",
            f"Delta: {r.candidate.entry_delta:.2f}",
            f"IV: {r.candidate.entry_iv:.0%}" if r.candidate.entry_iv is not None else "IV: n/a",
        ]
        if qa is not None:
            lines += [
                f"Probability of profit: {qa.probability_of_profit:.0%}",
                f"Expected value: ${qa.expected_value:,.2f}",
                f"Capital required: ${qa.capital_required:,.2f}",
                f"Max profit: {'UNLIMITED' if qa.max_profit == float('inf') else f'${qa.max_profit:,.2f}'}",
                f"Max loss: ${qa.max_loss:,.2f}",
                f"Return on capital: {qa.return_on_capital:.1%}",
            ]
        lines += [f"Thesis: {p.thesis}", ""]

    lines += [_SEP, "", "DEVIL'S ADVOCATE", ""]
    any_da = False
    for r in report.results:
        da = r.outcome.devils_advocate_review
        if da is None:
            continue
        any_da = True
        lines += [f"{r.candidate.proposal.ticker} {r.candidate.proposal.strategy.value}: [{da.review.verdict}] {da.review.why_not_thesis}", ""]
    if not any_da:
        lines += ["(no candidate reached Devil's Advocate review)"]

    lines += [_SEP, "", "RISK ENGINE", ""]
    any_risk = False
    for r in report.results:
        rd = r.outcome.risk_decision
        label = f"{r.candidate.proposal.ticker} {r.candidate.proposal.strategy.value}"
        if rd is None:
            lines += [f"{label}: (rejected before Risk Engine — {r.outcome.rejected_stage}: {r.outcome.reason})"]
            continue
        any_risk = True
        verdict = {RiskDecision.APPROVE: "APPROVE", RiskDecision.RESIZE: "RESIZE", RiskDecision.REJECT: "REJECT", RiskDecision.HALT: "REJECT (HALT)"}[rd.decision]
        lines += [f"{label}: {verdict} — {rd.message}"]
    if not any_risk and not report.results:
        lines += ["(no candidates evaluated)"]

    lines += ["", _SEP, "", "FIDELITY TRADE TICKET", ""]
    tickets = [(r, r.outcome.fidelity_ticket) for r in report.results if r.outcome.fidelity_ticket is not None]
    if report.no_trade_reason is not None:
        lines += ["NO TRADE", "", report.no_trade_reason]
    else:
        for r, ticket in tickets:
            lines += [render_morning_scan_ticket_section(ticket, r.outcome), ""]

    return "\n".join(lines)


def render_morning_scan_ticket_section(ticket: FidelityTradeTicket, outcome) -> str:
    """`render_ticket_text` (Step 8/12) already covers most of the
    template's required fields — this adds exactly the three it doesn't
    print (probability metrics, an explicit exit rule beyond profit
    target, and the Risk Engine's own verdict) rather than duplicating
    or modifying that already-tested function."""
    base = render_ticket_text(ticket)
    qa = outcome.quantitative_analysis
    extra = [
        "",
        "PROBABILITY OF PROFIT:",
        f"{qa.probability_of_profit:.0%}" if qa is not None else "n/a",
        "",
        "EXIT RULE:",
        ticket.loss_management_rule,
        "",
        "DTE MANAGEMENT RULE:",
        ticket.DTE_management_rule,
        "",
        "RISK ENGINE STATUS:",
        outcome.risk_decision.decision.value.upper() if outcome.risk_decision is not None else "n/a",
    ]
    return base + "\n" + "\n".join(extra)
