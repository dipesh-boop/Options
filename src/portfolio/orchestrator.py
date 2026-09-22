"""Step 22.4A Part 1: the thin PRODUCTION outer orchestrator over the
Portfolio Control Loop's independently-testable stages.

`src.portfolio.control_loop.run_control_cycle` (Part 12) deliberately
never phases in new-opportunity scanning (`src.portfolio.opportunity_scan`)
or pending-ticket monitoring (`src.portfolio.ticket_monitor`) -- its own
docstring says a "thin outer wrapper" is expected to run those separate,
independently-testable stages and merge their counts into the one
`ControlCycleRecord` it produces. Until this module, no such wrapper
existed anywhere in the repository, so `opportunities_scanned`/
`candidates_generated`/`candidates_rejected` stayed permanently
caller-supplied/zero and neither stage was ever invoked from a real
production entry point. `run_outer_cycle` is that wrapper.

**Still not a second Risk Engine or a second Lifecycle Engine.** This
module calls `run_control_cycle` (which itself calls the unmodified
Lifecycle Engine and Risk kill-switch), `scan_and_rank_opportunities`
(which itself calls the unmodified deterministic Risk Engine,
`src.risk.engine.evaluate_trade_proposal`), and `monitor_pending_tickets`
(which itself calls the unmodified Fidelity ticket state machine,
`src.brokers.fidelity.transition`) -- never overriding, duplicating, or
short-circuiting any of their verdicts. Nothing in this module places,
previews, cancels, or replaces a brokerage order of any kind, infers a
fill from a Risk approval, or starts the 90-day validation cohort.

**Priority: existing-position/risk monitoring > pending-ticket safety
monitoring > new-opportunity scanning** (Part 10). This module performs
no market-data I/O of its own -- `inputs.fetch_results` (for existing
positions) and `inputs.opportunity_scan.chains_by_ticker` (for
candidates) are supplied already-fetched by the caller, exactly like
every other module in this package -- so `run_control_cycle` itself is
NEVER gated on rate-limit headroom here: whatever data the caller
already fetched for open positions gets evaluated every cycle,
unconditionally. Only the two *optional*, lower-priority stages this
module adds are ever skipped under a constrained `RateLimitState`:
pending-ticket monitoring is gated at `RateLimitPriority
.P3_PENDING_TICKET_REPRICING`, new-opportunity scanning at
`RateLimitPriority.P4_OPPORTUNITY_SCANNING`. Since
`src.data.rate_limiter._PRIORITY_UTILIZATION_CEILING` throttles P4
before P3 before P2/P1/P0 as utilization climbs, opportunity scanning is
always the first thing sacrificed, ticket monitoring the second, and
existing-position/risk monitoring is never sacrificed by this module at
all -- it has no rate-limit gate here in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.brokers.fidelity import MAX_MARKET_DATA_AGE, FidelityTradeTicket
from src.data.option_chain import OptionChain
from src.data.provider import DEFAULT_MAX_QUOTE_AGE
from src.data.rate_limiter import RateLimitPriority, RateLimitState, degraded_priorities, may_proceed
from src.lifecycle.persistence import LifecycleStore
from src.llm.schemas import MarketRegimeLabel, StrategyType
from src.portfolio.actions import ControlLoopAction
from src.portfolio.alerts import ControlLoopAlert, generate_cycle_alerts
from src.portfolio.control_loop import ControlCycleInputs, ControlCycleResult, run_control_cycle
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot
from src.portfolio.opportunity_scan import OpportunityScanResult, scan_and_rank_opportunities
from src.portfolio.persistence import ControlLoopStore
from src.portfolio.ticket_monitor import DEFAULT_MAX_PRICE_DRIFT_PCT, TicketMonitorResult, monitor_pending_tickets
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.wheel.models import WheelPosition
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry

# Part 10's exact priority assignment for this module's two optional
# stages -- see module docstring for why existing-position/risk
# monitoring itself carries no rate-limit gate here at all.
_TICKET_MONITOR_PRIORITY = RateLimitPriority.P3_PENDING_TICKET_REPRICING
_OPPORTUNITY_SCAN_PRIORITY = RateLimitPriority.P4_OPPORTUNITY_SCANNING


@dataclass(frozen=True)
class TicketMonitorConfig:
    """Part 3's wiring inputs -- `pending_tickets` and their current net
    quotes are supplied by the caller (already fetched/priced this
    cycle), exactly like `ControlCycleInputs.fetch_results` is."""

    pending_tickets: tuple[FidelityTradeTicket, ...]
    current_quotes_by_trade_id: dict[str, tuple[float, float, datetime]] = field(default_factory=dict)
    max_quote_age: timedelta = MAX_MARKET_DATA_AGE
    max_price_drift_pct: float = DEFAULT_MAX_PRICE_DRIFT_PCT


@dataclass(frozen=True)
class OpportunityScanConfig:
    """Part 2's wiring inputs -- mirrors `scan_and_rank_opportunities`'s
    own parameter list exactly, so this module adds no new opportunity-
    screening logic of its own, only the wiring to call it."""

    universe: tuple[UniverseEntry, ...]
    chains_by_ticker: dict[str, OptionChain]
    strategies: tuple[StrategyType, ...]
    quant_filter: QuantFilterConfig
    market_regime: MarketRegimeLabel
    broker_capabilities: BrokerCapabilities | None
    no_trade_hurdle: float = 0.0
    proposal_id_prefix: str = "control-loop-scan"


@dataclass(frozen=True)
class OuterCycleInputs:
    """Every field `ControlCycleInputs` itself takes, minus the three
    opportunity-count fields (this module computes and supplies those),
    plus the two optional stage configs and the rate-limit state that
    decides whether they run this cycle."""

    cycle_id: str
    as_of: datetime
    portfolio: Portfolio
    limits: RiskLimitsConfig
    provider: str
    provider_health_status: str
    is_trading_day: bool
    is_market_open: bool
    fetch_results: dict[str, OptionChain | Exception]
    lifecycle_store: LifecycleStore
    control_loop_store: ControlLoopStore
    policy_name_for_position: dict[str, str] = field(default_factory=dict)
    wheel_id_for_position: dict[str, str] = field(default_factory=dict)
    extra_monitoring_inputs: dict[str, dict] = field(default_factory=dict)
    wheel_positions: tuple[WheelPosition, ...] = ()
    current_underlying_prices_for_exposure: dict[str, float] = field(default_factory=dict)
    max_quote_age: timedelta = DEFAULT_MAX_QUOTE_AGE
    rate_limit_state: RateLimitState | None = None
    ticket_monitor: TicketMonitorConfig | None = None
    opportunity_scan: OpportunityScanConfig | None = None
    # Explicit caller override (e.g. "don't scan outside market hours") --
    # distinct from a rate-limit-driven skip, but reported identically
    # honestly in the result below (never silently conflated with "ran
    # and found nothing").
    skip_ticket_monitor: bool = False
    skip_opportunity_scan: bool = False


@dataclass(frozen=True)
class OuterCycleResult:
    control_result: ControlCycleResult
    ticket_monitor_result: TicketMonitorResult | None
    ticket_monitor_skipped_reason: str | None
    opportunity_scan_result: OpportunityScanResult | None
    opportunity_scan_skipped_reason: str | None
    opportunity_decision_snapshot: PortfolioControlDecisionSnapshot | None
    new_alerts: tuple[ControlLoopAlert, ...]
    degraded_priorities: tuple[RateLimitPriority, ...]


def _run_ticket_monitor_stage(
    inputs: OuterCycleInputs,
) -> tuple[TicketMonitorResult | None, str | None]:
    if inputs.skip_ticket_monitor:
        return None, "pending-ticket monitoring explicitly skipped by caller this cycle"
    cfg = inputs.ticket_monitor
    if cfg is None or not cfg.pending_tickets:
        return None, "no pending tickets configured for this cycle"
    if not may_proceed(_TICKET_MONITOR_PRIORITY, inputs.rate_limit_state):
        return None, (
            f"rate-limit budget constrained at {_TICKET_MONITOR_PRIORITY.name} -- "
            "existing-position risk monitoring takes priority this cycle"
        )
    result = monitor_pending_tickets(
        list(cfg.pending_tickets), as_of=inputs.as_of,
        current_quotes_by_trade_id=cfg.current_quotes_by_trade_id,
        max_quote_age=cfg.max_quote_age, max_price_drift_pct=cfg.max_price_drift_pct,
    )
    return result, None


def _run_opportunity_scan_stage(
    inputs: OuterCycleInputs,
) -> tuple[OpportunityScanResult | None, str | None, int, int, int]:
    """Returns `(result, skipped_reason, opportunities_scanned,
    candidates_generated, candidates_rejected)` -- the three counts are
    `0` whenever the stage didn't run, never fabricated (Part 2: "do not
    fabricate counts")."""
    if inputs.skip_opportunity_scan:
        return None, "new-opportunity scanning explicitly skipped by caller this cycle", 0, 0, 0
    cfg = inputs.opportunity_scan
    if cfg is None or not cfg.universe:
        return None, "no opportunity-scan universe configured for this cycle", 0, 0, 0
    if not may_proceed(_OPPORTUNITY_SCAN_PRIORITY, inputs.rate_limit_state):
        return None, (
            f"rate-limit budget constrained at {_OPPORTUNITY_SCAN_PRIORITY.name} -- "
            "existing-position risk monitoring and pending-ticket safety monitoring take priority this cycle"
        ), 0, 0, 0

    opportunities_scanned = sum(1 for e in cfg.universe if e.ticker in cfg.chains_by_ticker)
    result = scan_and_rank_opportunities(
        list(cfg.universe), cfg.chains_by_ticker, list(cfg.strategies), cfg.quant_filter,
        inputs.limits, inputs.portfolio, cfg.market_regime, cfg.broker_capabilities,
        now=inputs.as_of, proposal_id_prefix=cfg.proposal_id_prefix, no_trade_hurdle=cfg.no_trade_hurdle,
    )
    return result, None, opportunities_scanned, result.candidates_generated, result.candidates_rejected


def _build_opportunity_decision_snapshot(
    inputs: OuterCycleInputs, control_result: ControlCycleResult, scan_result: OpportunityScanResult,
) -> PortfolioControlDecisionSnapshot | None:
    """One additional decision point for the cycle's best Risk-approved/
    resized candidate (Part 30's own "left None for a new-opportunity-
    only decision point" carve-out) -- `None` when the scan found nothing
    worth surfacing (`best is None`, the explicit CASH/NO_TRADE outcome,
    Part 2: "CASH/NO_TRADE remains a valid result"), never a fabricated
    snapshot for "nothing." `scan_and_rank_opportunities` only ever sets
    `best` from its own `survivors` list -- entries whose `risk_decision`
    already cleared the Risk Engine (APPROVE/RESIZE) -- so this never
    needs to re-check that decision; it only relabels it in this layer's
    own `ControlLoopAction` vocabulary (`REVIEW`: a human must still
    act -- Risk approval is never execution, Part 3)."""
    best = scan_result.best
    if best is None:
        return None
    valuation = control_result.valuation
    return PortfolioControlDecisionSnapshot(
        cycle_id=inputs.cycle_id, timestamp=inputs.as_of,
        is_trading_day=inputs.is_trading_day, is_market_open=inputs.is_market_open,
        provider=inputs.provider, provider_health_status=inputs.provider_health_status,
        portfolio_nav=valuation.nav, portfolio_cash=valuation.cash,
        portfolio_deployed_pct=valuation.capital_deployed_pct,
        portfolio_drawdown_pct=valuation.current_drawdown_pct,
        portfolio_delta=valuation.portfolio_delta, portfolio_gamma=valuation.portfolio_gamma,
        portfolio_theta=valuation.portfolio_theta, portfolio_vega=valuation.portfolio_vega,
        risk_decision=best.risk_decision.value, risk_reason=best.risk_reason,
        recommended_action=ControlLoopAction.REVIEW,
        reason_codes=(best.risk_reason,),
        opportunity_proposal_id=best.candidate.proposal.proposal_id,
        new_opportunity_comparison=(
            f"{best.candidate.proposal.ticker} {best.candidate.proposal.strategy.value}: "
            f"risk-adjusted return {best.risk_adjusted_return:.4f}"
            if best.risk_adjusted_return is not None
            else f"{best.candidate.proposal.ticker} {best.candidate.proposal.strategy.value}: new candidate identified"
        ),
        human_action_required=True,
    )


def run_outer_cycle(inputs: OuterCycleInputs) -> OuterCycleResult:
    """One full production cycle: pending-ticket monitoring (Part 3,
    priority-gated) -> new-opportunity scanning (Part 2, priority-gated,
    its counts feeding `ControlCycleInputs`) -> the existing, UNMODIFIED
    `run_control_cycle` (Part 12, never rate-limit-gated here) ->
    persisting this cycle's exposure snapshot (Part 12's own
    `ControlCycleResult.exposure` was never durably saved anywhere before
    this module) -> the opportunity decision snapshot, if any -> alert
    generation (Part 4, deduplicated against whatever's already
    unresolved in `inputs.control_loop_store`) -> alert persistence.

    Never raises for a degraded/missing optional stage -- only a
    genuinely systemic failure (malformed `inputs.limits`/`inputs.portfolio`,
    exactly like `run_control_cycle` itself) propagates."""
    ticket_monitor_result, ticket_skip_reason = _run_ticket_monitor_stage(inputs)
    opportunity_scan_result, opportunity_skip_reason, opportunities_scanned, candidates_generated, candidates_rejected = (
        _run_opportunity_scan_stage(inputs)
    )

    control_inputs = ControlCycleInputs(
        cycle_id=inputs.cycle_id, as_of=inputs.as_of, portfolio=inputs.portfolio, limits=inputs.limits,
        provider=inputs.provider, provider_health_status=inputs.provider_health_status,
        is_trading_day=inputs.is_trading_day, is_market_open=inputs.is_market_open,
        fetch_results=inputs.fetch_results, lifecycle_store=inputs.lifecycle_store,
        control_loop_store=inputs.control_loop_store,
        policy_name_for_position=inputs.policy_name_for_position, wheel_id_for_position=inputs.wheel_id_for_position,
        extra_monitoring_inputs=inputs.extra_monitoring_inputs, wheel_positions=inputs.wheel_positions,
        current_underlying_prices_for_exposure=inputs.current_underlying_prices_for_exposure,
        max_quote_age=inputs.max_quote_age,
        opportunities_scanned=opportunities_scanned, candidates_generated=candidates_generated,
        candidates_rejected=candidates_rejected,
    )
    control_result = run_control_cycle(control_inputs)

    # Part 12's own `ControlCycleResult.exposure` was never durably
    # persisted by `run_control_cycle` itself -- this is the one place
    # that now happens, so a restarted dashboard can honestly reload it
    # (Part 5/6).
    inputs.control_loop_store.save_exposure_snapshot(inputs.cycle_id, control_result.exposure)

    opportunity_snapshot = None
    if opportunity_scan_result is not None:
        opportunity_snapshot = _build_opportunity_decision_snapshot(inputs, control_result, opportunity_scan_result)
        if opportunity_snapshot is not None:
            inputs.control_loop_store.append_decision_snapshot(opportunity_snapshot)

    all_snapshots = list(control_result.decision_snapshots)
    if opportunity_snapshot is not None:
        all_snapshots.append(opportunity_snapshot)

    degraded = degraded_priorities(inputs.rate_limit_state)
    new_alerts = generate_cycle_alerts(
        cycle_record=control_result.cycle_record, decision_snapshots=all_snapshots,
        drawdown_zone=control_result.valuation.drawdown_zone,
        existing=inputs.control_loop_store.all_unresolved_alerts(), now=inputs.as_of,
        ticket_monitor_result=ticket_monitor_result, rate_limit_degraded=bool(degraded),
    )
    for alert in new_alerts:
        inputs.control_loop_store.save_alert(alert)

    return OuterCycleResult(
        control_result=control_result,
        ticket_monitor_result=ticket_monitor_result, ticket_monitor_skipped_reason=ticket_skip_reason,
        opportunity_scan_result=opportunity_scan_result, opportunity_scan_skipped_reason=opportunity_skip_reason,
        opportunity_decision_snapshot=opportunity_snapshot,
        new_alerts=tuple(new_alerts), degraded_priorities=degraded,
    )
