"""API request/response shapes for the dashboard (Step 18). Response
models reuse the existing, already-validated domain types directly
(`DevilsAdvocateReview`, `RiskDecision`, `ReasonCode`, `TicketStatus`)
rather than re-declaring their fields a second time — this module adds
shape (grouping, DTE, a credit/debit guard label) but invents no new
numeric computation of its own.

Every request model here accepts only what a human reports about their
own action (a limit they entered, a fill price, a reason) — never a
Fidelity username, password, MFA code, or session cookie. See
`tests/unit/dashboard/test_app_security.py` for the structural proof.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from src.brokers.fidelity import FidelityLegAction, TicketStatus
from src.data.option_chain import OptionRight
from src.lifecycle.persistence import LifecyclePositionRecord
from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.snapshot import LifecycleDecisionSnapshot
from src.llm.schemas import DevilsAdvocateReview
from src.portfolio.alerts import ControlLoopAlert
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.exposure import PortfolioExposureSnapshot
from src.risk.reason_codes import ReasonCode, RiskDecision
from src.wheel.accounting import WheelEconomicsSummary
from src.wheel.state import WheelState


class LegView(BaseModel):
    action: FidelityLegAction
    put_call: OptionRight
    strike: float
    contracts: int


class QuoteView(BaseModel):
    net_bid: float
    net_ask: float
    net_mid: float
    underlying_price: float
    quote_timestamp: datetime


class RiskEngineDecisionView(BaseModel):
    decision: RiskDecision
    reason_codes: list[ReasonCode]
    message: str
    max_profit: float | None = None
    max_loss: float | None = None
    capital_required: float | None = None
    worst_case_stress_loss_pct_of_nav: float | None = None


class ProbabilityMetricsView(BaseModel):
    probability_of_profit: float
    expected_value: float
    annualized_roc: float


class OpportunityView(BaseModel):
    """One row of TODAY'S OPPORTUNITIES."""

    model_config = ConfigDict(use_enum_values=False)

    trade_id: str
    ticker: str
    strategy: str
    expiration: date
    dte: int
    legs: list[LegView]
    contracts: int
    quote: QuoteView
    target_limit: float
    minimum_acceptable_price: float
    price_guard_label: str  # "minimum acceptable credit" | "maximum acceptable debit"
    is_credit: bool
    max_profit: float
    max_loss: float
    breakeven: float
    breakeven_upper: float | None = None
    capital_required: float
    return_on_capital: float
    probability_metrics: ProbabilityMetricsView | None
    thesis: str
    devils_advocate: DevilsAdvocateReview | None
    risk_engine_decision: RiskEngineDecisionView | None
    status: TicketStatus
    status_updated_at: datetime
    copy_enabled: bool
    order_entry: "OrderEntryView | None" = None
    rejection_reason: str | None = None
    cancellation_reason: str | None = None


class OrderEntryView(BaseModel):
    actual_limit_entered: float
    contracts: int
    entered_at: datetime
    entered_by: str


OpportunityView.model_rebuild()


class PortfolioHeaderView(BaseModel):
    nav: float
    daily_pnl: float | None
    ytd_return_pct: float | None
    cash: float
    capital_deployed_pct: float
    current_drawdown_pct: float
    portfolio_delta: float | None
    portfolio_theta: float | None
    portfolio_vega: float | None


class ConcentrationEntryView(BaseModel):
    label: str
    exposure_pct: float
    limit_pct: float
    breached: bool


class CorrelationClusterView(BaseModel):
    tickers: tuple[str, str]
    correlation: float


class RiskPanelResponseView(BaseModel):
    state: str
    state_reason: str
    capital_utilization_pct: float
    cash_reserve_pct: float
    underlying_concentration: list[ConcentrationEntryView]
    sector_concentration: list[ConcentrationEntryView]
    correlation_clusters: list[CorrelationClusterView]
    correlation_tracked: bool
    current_drawdown_pct: float
    drawdown_zone: str


class AuditEventView(BaseModel):
    event_id: str
    trade_id: str | None
    event_type: str
    at: datetime
    actor: str
    detail: str


# ---------------------------------------------------------------- requests


class LegQuoteInput(BaseModel):
    strike: float = Field(gt=0)
    right: OptionRight
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)
    iv: float | None = None


class RefreshPriceRequest(BaseModel):
    """The dashboard has no live market-data connection of its own (the
    same boundary every workflow in this codebase holds to) — a caller
    supplies the freshly-fetched quote fields, which this endpoint folds
    into a real `OptionChain` before re-running Quant/Risk. One quote
    per leg the ticket already has (matched by strike+right); the
    request need not restate which legs exist."""

    underlying_price: float = Field(gt=0)
    underlying_bid: float = Field(gt=0)
    underlying_ask: float = Field(gt=0)
    quote_timestamp: datetime
    leg_quotes: list[LegQuoteInput] = Field(min_length=1, max_length=4)


class MarkOrderEnteredRequest(BaseModel):
    actual_limit_entered: float = Field(gt=0)
    contracts: int = Field(gt=0)
    entered_by: str = Field(min_length=1, max_length=100)
    entered_at: datetime | None = None


class RecordFillRequest(BaseModel):
    status: str = Field(pattern="^(FILLED|PARTIALLY_FILLED)$")
    fill_price: float = Field(gt=0)
    contracts_filled: int = Field(gt=0)
    confirmed_by: str = Field(min_length=1, max_length=100)
    confirmed_at: datetime | None = None


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=100)


class RejectTradeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=100)


# ------------------------------------------------------------- view builders
# Pure functions: internal dataclass/domain-model -> API response shape.
# No computation happens here beyond grouping already-computed fields.

from src.dashboard.models import DashboardState, OpportunityRecord  # noqa: E402
from src.dashboard.models import AuditEvent as _AuditEvent  # noqa: E402
from src.dashboard.risk_state import RiskPanelView as _RiskPanelView  # noqa: E402
from src.dashboard.risk_state import build_risk_panel as _build_risk_panel  # noqa: E402
from src.risk.drawdown import current_drawdown_pct as _current_drawdown_pct  # noqa: E402
from src.risk.portfolio_risk import capital_deployed_pct as _capital_deployed_pct  # noqa: E402


def build_opportunity_view(record: OpportunityRecord, now: datetime) -> OpportunityView:
    ticket = record.ticket
    is_credit = ticket.estimated_credit_debit >= 0
    qa = record.quantitative_analysis
    rd = record.risk_decision
    da = record.devils_advocate_review
    return OpportunityView(
        trade_id=record.trade_id,
        ticker=ticket.ticker,
        strategy=ticket.strategy,
        expiration=ticket.expiration,
        dte=(ticket.expiration - now.date()).days,
        legs=[LegView(action=leg.action, put_call=leg.put_call, strike=leg.strike, contracts=leg.contracts) for leg in ticket.legs],
        contracts=ticket.quantity,
        quote=QuoteView(
            net_bid=ticket.net_bid, net_ask=ticket.net_ask, net_mid=ticket.net_mid,
            underlying_price=ticket.underlying_price, quote_timestamp=ticket.market_data_timestamp,
        ),
        target_limit=abs(ticket.limit_price),
        minimum_acceptable_price=abs(ticket.minimum_acceptable_price),
        price_guard_label="minimum acceptable credit" if is_credit else "maximum acceptable debit",
        is_credit=is_credit,
        max_profit=ticket.max_profit,
        max_loss=ticket.max_loss,
        breakeven=ticket.breakeven,
        breakeven_upper=ticket.breakeven_upper,
        capital_required=ticket.capital_at_risk,
        return_on_capital=ticket.return_on_capital,
        probability_metrics=(
            ProbabilityMetricsView(
                probability_of_profit=qa.probability_of_profit, expected_value=qa.expected_value, annualized_roc=qa.annualized_roc,
            )
            if qa is not None else None
        ),
        thesis=record.proposal.thesis,
        devils_advocate=da.review if da is not None else None,
        risk_engine_decision=(
            RiskEngineDecisionView(
                decision=rd.decision, reason_codes=rd.reason_codes, message=rd.message, max_profit=rd.max_profit,
                max_loss=rd.max_loss, capital_required=rd.capital_required,
                worst_case_stress_loss_pct_of_nav=rd.worst_case_stress_loss_pct_of_nav,
            )
            if rd is not None else None
        ),
        status=ticket.status,
        status_updated_at=ticket.status_updated_at,
        copy_enabled=(ticket.status == TicketStatus.AWAITING_HUMAN),
        order_entry=(
            OrderEntryView(
                actual_limit_entered=record.order_entry.actual_limit_entered, contracts=record.order_entry.contracts,
                entered_at=record.order_entry.entered_at, entered_by=record.order_entry.entered_by,
            )
            if record.order_entry is not None else None
        ),
        rejection_reason=record.rejection_reason,
        cancellation_reason=record.cancellation_reason,
    )


def build_portfolio_header_view(state: DashboardState) -> PortfolioHeaderView:
    portfolio = state.portfolio
    return PortfolioHeaderView(
        nav=portfolio.nav,
        daily_pnl=state.daily_pnl,
        ytd_return_pct=state.ytd_return_pct,
        cash=portfolio.cash,
        capital_deployed_pct=_capital_deployed_pct(portfolio),
        current_drawdown_pct=_current_drawdown_pct(portfolio),
        portfolio_delta=state.portfolio_net_delta,
        portfolio_theta=state.portfolio_net_theta,
        portfolio_vega=state.portfolio_net_vega,
    )


def build_risk_panel_view(panel: _RiskPanelView) -> RiskPanelResponseView:
    return RiskPanelResponseView(
        state=panel.state.value,
        state_reason=panel.state_reason,
        capital_utilization_pct=panel.capital_utilization_pct,
        cash_reserve_pct=panel.cash_reserve_pct,
        underlying_concentration=[
            ConcentrationEntryView(label=e.label, exposure_pct=e.exposure_pct, limit_pct=e.limit_pct, breached=e.breached)
            for e in panel.underlying_concentration
        ],
        sector_concentration=[
            ConcentrationEntryView(label=e.label, exposure_pct=e.exposure_pct, limit_pct=e.limit_pct, breached=e.breached)
            for e in panel.sector_concentration
        ],
        correlation_clusters=[
            CorrelationClusterView(tickers=c.tickers, correlation=c.correlation) for c in panel.correlation_clusters
        ],
        correlation_tracked=panel.correlation_tracked,
        current_drawdown_pct=panel.current_drawdown_pct,
        drawdown_zone=panel.drawdown_zone.value,
    )


def build_audit_event_view(event: _AuditEvent) -> AuditEventView:
    return AuditEventView(
        event_id=event.event_id, trade_id=event.trade_id, event_type=event.event_type.value,
        at=event.at, actor=event.actor, detail=event.detail,
    )


# --------------------------------------------------- data provider health


class DataProviderHealthView(BaseModel):
    """Step 22.1: so the owner never has to guess what kind of market
    data the system is using -- REAL DATA CONNECTED / MOCK DATA / REAL
    DATA UNAVAILABLE, plus whether options data is OPRA or indicative/
    delayed. Read-only; this route never accepts input."""

    provider_selected: str
    connection_status: str
    authenticated: bool | None
    equity_data_available: bool
    options_data_available: bool
    equity_feed_type: str | None
    options_feed_type: str | None
    opra_entitled: bool | None
    market_open: bool
    market_status_detail: str
    checked_at: datetime
    last_successful_fetch_at: datetime | None
    equity_quote_age_seconds: float | None
    errors: list[str]


def build_data_provider_health_view(report) -> DataProviderHealthView:
    return DataProviderHealthView(
        provider_selected=report.provider_selected, connection_status=report.connection_status,
        authenticated=report.authenticated, equity_data_available=report.equity_data_available,
        options_data_available=report.options_data_available, equity_feed_type=report.equity_feed_type,
        options_feed_type=report.options_feed_type, opra_entitled=report.opra_entitled,
        market_open=report.market_open, market_status_detail=report.market_status_detail,
        checked_at=report.checked_at, last_successful_fetch_at=report.last_successful_fetch_at,
        equity_quote_age_seconds=report.equity_quote_age_seconds, errors=list(report.errors),
    )


# ------------------------------------------------------------------ wheels


class WheelCycleView(BaseModel):
    cycle_id: str
    strike: float
    expiration: date
    contracts: int
    premium_received_per_share: float
    is_open: bool
    close_reason: str | None = None
    realized_pnl: float | None = None
    below_acquisition_basis: bool = False
    below_economic_basis: bool = False


class WheelView(BaseModel):
    """Step 22.2, Part 19: read-only Wheel visibility. Clearly labeled
    `RESEARCH / PAPER` by the caller supplying it (the dashboard has
    exactly one data source for Wheel state -- src.wheel.persistence --
    and no code path anywhere in this package that could confuse it with
    a manual Fidelity execution ticket). No automated-execution action is
    exposed anywhere on this view or the route that serves it."""

    wheel_id: str
    ticker: str
    state: str
    started_at: datetime
    completed_at: datetime | None
    days_active: int
    active_csp: WheelCycleView | None
    active_cc: WheelCycleView | None
    shares_owned: int
    acquisition_basis_per_share: float | None
    economic_basis_per_share: float | None
    current_underlying_price: float | None
    current_market_value: float | None
    unrealized_stock_pnl: float
    total_premium_collected: float
    total_net_pnl: float
    capital_committed: float
    max_capital_committed: float
    return_on_committed_capital: float | None
    csp_cycle_count: int
    cc_cycle_count: int
    next_decision: str
    risk_status: str


def _wheel_cycle_view(cycle) -> WheelCycleView:
    return WheelCycleView(
        cycle_id=cycle.cycle_id, strike=cycle.strike, expiration=cycle.expiration, contracts=cycle.contracts,
        premium_received_per_share=cycle.premium_received_per_share, is_open=cycle.is_open,
        close_reason=cycle.close_reason.value if cycle.close_reason else None, realized_pnl=cycle.realized_pnl,
        below_acquisition_basis=getattr(cycle, "below_acquisition_basis", False),
        below_economic_basis=getattr(cycle, "below_economic_basis", False),
    )


_NEXT_DECISION_BY_STATE = {
    WheelState.WHEEL_CANDIDATE: "Awaiting eligibility/Risk Engine review before opening a CSP.",
    WheelState.CSP_OPEN: "Monitoring the open CSP for expiration, early close, or assignment.",
    WheelState.ASSIGNED_SHARES: "Assigned -- ready to evaluate a covered call.",
    WheelState.CC_ELIGIBLE: "Holding shares; evaluate a covered call or continue holding (NO_CC_TRADE is valid).",
    WheelState.CC_OPEN: "Monitoring the open covered call for expiration, early close, or call-away.",
    WheelState.CC_EXPIRED: "Covered call expired worthless; re-evaluating for a new covered call.",
    WheelState.CC_CLOSED: "Covered call bought back; reassessing.",
    WheelState.SHARES_CALLED_AWAY: "Shares called away; finalizing the Wheel.",
    WheelState.WHEEL_COMPLETE: "Wheel complete. A new Wheel must compete again from scratch.",
    WheelState.CSP_EXPIRED: "CSP expired worthless, unassigned. Wheel finished without ever holding shares.",
    WheelState.CSP_CLOSED: "CSP bought to close, unassigned. Wheel finished without ever holding shares.",
    WheelState.WHEEL_EXITED: "Manually exited.",
    WheelState.WHEEL_HALTED: "Halted -- awaiting manual review before any further action.",
    WheelState.WHEEL_REJECTED: "Rejected before any order was placed.",
}


def build_wheel_view(wheel, *, current_underlying_price: float | None, now: datetime) -> WheelView:
    from src.wheel.accounting import summarize_wheel_economics

    summary: WheelEconomicsSummary = summarize_wheel_economics(wheel, current_underlying_price=current_underlying_price, now=now)
    basis_flag = ""
    if wheel.open_cc_cycle is not None and wheel.open_cc_cycle.below_acquisition_basis:
        basis_flag = " [BELOW_ACQUISITION_BASIS]" + (" [BELOW_ECONOMIC_BASIS]" if wheel.open_cc_cycle.below_economic_basis else "")
    risk_status = "normal" if wheel.state != WheelState.WHEEL_HALTED else "halted"
    if basis_flag:
        risk_status = "flagged" if risk_status == "normal" else risk_status

    return WheelView(
        wheel_id=wheel.wheel_id, ticker=wheel.ticker, state=wheel.state.value,
        started_at=wheel.started_at, completed_at=wheel.completed_at, days_active=summary.days_in_wheel,
        active_csp=_wheel_cycle_view(wheel.open_csp_cycle) if wheel.open_csp_cycle is not None else None,
        active_cc=_wheel_cycle_view(wheel.open_cc_cycle) if wheel.open_cc_cycle is not None else None,
        shares_owned=summary.shares_owned, acquisition_basis_per_share=summary.acquisition_basis_per_share,
        economic_basis_per_share=summary.economic_basis_per_share, current_underlying_price=summary.current_underlying_price,
        current_market_value=summary.current_market_value, unrealized_stock_pnl=summary.unrealized_stock_pnl,
        total_premium_collected=summary.total_premium, total_net_pnl=summary.total_net_pnl,
        capital_committed=summary.capital_committed, max_capital_committed=summary.max_capital_committed,
        return_on_committed_capital=summary.return_on_committed_capital, csp_cycle_count=summary.csp_cycle_count,
        cc_cycle_count=summary.cc_cycle_count,
        next_decision=_NEXT_DECISION_BY_STATE.get(wheel.state, "Under review.") + basis_flag,
        risk_status=risk_status,
    )


# ---------------------------------------------------------------------------
# Step 22.3, Part 22: read-only Active Positions / Lifecycle visibility.
# ---------------------------------------------------------------------------


class LifecycleStatusIndicator(str, Enum):
    """Part 22's ten named indicators, plus `REGIME_REVIEW`/
    `ASSIGNMENT_REVIEW` — Part 22's list has no single-word fit for
    Part 9 (regime) or Part 12 (assignment) review conditions, the same
    "at minimum" spirit Part 23 states explicitly for alert types."""

    HOLD = "hold"
    PROFIT_TARGET = "profit_target"
    LOSS_REVIEW = "loss_review"
    TIME_EXIT = "time_exit"
    DELTA_REVIEW = "delta_review"
    VOLATILITY_REVIEW = "volatility_review"
    EVENT_RISK = "event_risk"
    LIQUIDITY_WARNING = "liquidity_warning"
    RISK_EXIT = "risk_exit"
    DATA_INSUFFICIENT = "data_insufficient"
    REGIME_REVIEW = "regime_review"
    ASSIGNMENT_REVIEW = "assignment_review"


_DELTA_TRIGGER_NAMES = frozenset({"delta_threshold", "delta_close_threshold"})


def status_indicator_for(category: str, winning_trigger_names: tuple[str, ...]) -> LifecycleStatusIndicator:
    """Maps a `src.lifecycle.precedence.ResolvedAction`'s own category
    (already the single source of truth for "what is actually
    happening" per Part 18) to one Part 22 display indicator. Never
    re-derives the underlying decision — this is display labeling only."""
    if category == "system_data_safety":
        return LifecycleStatusIndicator.DATA_INSUFFICIENT
    if category == "risk_halt":
        return LifecycleStatusIndicator.RISK_EXIT
    if category == "hard_loss_exposure":
        return LifecycleStatusIndicator.LOSS_REVIEW
    if category in ("assignment_expiration", "optional_adjustment"):
        return LifecycleStatusIndicator.ASSIGNMENT_REVIEW
    if category == "event_risk":
        return LifecycleStatusIndicator.EVENT_RISK
    if category == "liquidity_risk":
        return LifecycleStatusIndicator.LIQUIDITY_WARNING
    if category == "time_exit":
        return LifecycleStatusIndicator.TIME_EXIT
    if category == "profit_target":
        return LifecycleStatusIndicator.PROFIT_TARGET
    if category == "delta_volatility_review":
        if any(name in _DELTA_TRIGGER_NAMES for name in winning_trigger_names):
            return LifecycleStatusIndicator.DELTA_REVIEW
        if "regime_change_action" in winning_trigger_names:
            return LifecycleStatusIndicator.REGIME_REVIEW
        return LifecycleStatusIndicator.VOLATILITY_REVIEW
    return LifecycleStatusIndicator.HOLD


class LifecyclePositionView(BaseModel):
    """Part 22's exact per-position field list. No execution control of
    any kind is exposed here or on the route that serves it — this is
    read-only visibility, exactly like `WheelView`."""

    trade_id: str
    strategy: str
    ticker: str
    entry_date: date
    entry_dte: int | None
    current_dte: int | None
    entry_premium: float | None
    current_value: float | None
    unrealized_pnl: float
    unrealized_pnl_pct: float | None
    mfe: float
    mae: float
    delta: float | None
    iv_change: float | None
    management_policy: str
    current_state: str
    status_indicator: LifecycleStatusIndicator
    next_scheduled_review_dte: int | None
    recommended_action: str
    risk_status: str
    data_is_fresh: bool


def build_lifecycle_position_view(
    record: LifecyclePositionRecord,
    *,
    latest_snapshot: LifecycleDecisionSnapshot | None,
    resolved: ResolvedAction | None,
    entry_dte: int | None,
    entry_premium: float | None,
    next_scheduled_review_dte: int | None,
) -> LifecyclePositionView:
    """Builds the view purely from already-computed inputs: `record`
    (this package's own persisted state), the most recent
    `LifecycleDecisionSnapshot` for this trade (Part 17's own record of
    what was observed and decided last), and the `ResolvedAction` from
    that same evaluation (for the status indicator). Nothing here
    recomputes a price, Greek, or P&L figure."""
    unrealized_pnl = latest_snapshot.unrealized_pnl if latest_snapshot is not None else 0.0
    current_value = latest_snapshot.option_prices.get("mid") if latest_snapshot is not None else None
    unrealized_pnl_pct = (
        unrealized_pnl / abs(entry_premium) if entry_premium not in (None, 0.0) else None
    )
    status = (
        status_indicator_for(resolved.category, resolved.winning_trigger_names)
        if resolved is not None
        else LifecycleStatusIndicator.HOLD
    )
    return LifecyclePositionView(
        trade_id=record.trade_id,
        strategy=record.strategy_kind.value,
        ticker=record.ticker,
        entry_date=record.created_at.date(),
        entry_dte=entry_dte,
        current_dte=(latest_snapshot.dte if latest_snapshot is not None else None),
        entry_premium=entry_premium,
        current_value=current_value,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        mfe=record.excursion.mfe,
        mae=record.excursion.mae,
        delta=(latest_snapshot.delta if latest_snapshot is not None else None),
        iv_change=None,
        management_policy=record.management_policy_name,
        current_state=record.current_state.value,
        status_indicator=status,
        next_scheduled_review_dte=next_scheduled_review_dte,
        recommended_action=(resolved.reason if resolved is not None else "no lifecycle trigger fired"),
        risk_status=(latest_snapshot.risk_status if latest_snapshot is not None else "unknown"),
        data_is_fresh=(latest_snapshot.data_is_fresh if latest_snapshot is not None else False),
    )


# ---------------------------------------------------------------------------
# Step 22.4, Parts 33-34: read-only Portfolio Control Loop visibility.
# **PAPER TRADING. FIDELITY EXECUTION IS MANUAL.** Every view/route in
# this section is read-only -- there is no POST/PUT route anywhere for
# the control loop, no route that starts/stops/reconfigures a cycle, and
# nothing here can place, preview, or modify a broker order of any kind
# (Part 34's explicit "no Buy/Sell/Submit buttons connected to any
# broker" requirement). A cycle is driven by whatever schedules
# `src.portfolio.control_loop.run_control_cycle` (not this package); the
# dashboard only ever displays its most recent recorded output.
# ---------------------------------------------------------------------------


class ControlCycleStatusView(BaseModel):
    cycle_id: str
    started_at: datetime
    completed_at: datetime | None
    is_complete: bool
    market_open: bool
    provider: str
    provider_health_status: str
    symbols_requested: int
    symbols_successful: int
    symbols_failed: int
    positions_evaluated: int
    lifecycle_triggers: int
    risk_events: int
    recommendations_created: int
    opportunities_scanned: int
    candidates_generated: int
    candidates_rejected: int
    degraded_mode: bool
    halt_state: bool
    had_errors: bool
    errors: tuple[str, ...]


def build_control_cycle_status_view(record: ControlCycleRecord) -> ControlCycleStatusView:
    return ControlCycleStatusView(
        cycle_id=record.cycle_id,
        started_at=record.started_at,
        completed_at=record.completed_at,
        is_complete=record.is_complete,
        market_open=record.market_open,
        provider=record.provider,
        provider_health_status=record.provider_health_status,
        symbols_requested=len(record.symbols_requested),
        symbols_successful=len(record.symbols_successful),
        symbols_failed=len(record.symbols_failed),
        positions_evaluated=record.positions_evaluated,
        lifecycle_triggers=record.lifecycle_triggers,
        risk_events=record.risk_events,
        recommendations_created=record.recommendations_created,
        opportunities_scanned=record.opportunities_scanned,
        candidates_generated=record.candidates_generated,
        candidates_rejected=record.candidates_rejected,
        degraded_mode=record.degraded_mode,
        halt_state=record.halt_state,
        had_errors=record.had_errors,
        errors=record.errors,
    )


class PortfolioExposureView(BaseModel):
    as_of: datetime
    underlying_exposure_pct: dict[str, float]
    sector_exposure_pct: dict[str, float]
    strategy_exposure_pct: dict[str, float]
    directional_exposure: str
    portfolio_delta: float | None
    volatility_exposure: str
    portfolio_vega: float | None
    short_option_capital_pct: float
    assignment_risk_position_ids: tuple[str, ...]
    wheel_cash_commitment_pct: float
    owned_share_exposure_pct: float
    covered_call_encumbered_shares: dict[str, int]


def build_exposure_view(exposure: PortfolioExposureSnapshot) -> PortfolioExposureView:
    return PortfolioExposureView(
        as_of=exposure.as_of,
        underlying_exposure_pct=exposure.underlying_exposure_pct,
        sector_exposure_pct=exposure.sector_exposure_pct,
        strategy_exposure_pct=exposure.strategy_exposure_pct,
        directional_exposure=exposure.directional_exposure,
        portfolio_delta=exposure.portfolio_delta,
        volatility_exposure=exposure.volatility_exposure,
        portfolio_vega=exposure.portfolio_vega,
        short_option_capital_pct=exposure.short_option_capital_pct,
        assignment_risk_position_ids=exposure.assignment_risk_position_ids,
        wheel_cash_commitment_pct=exposure.wheel_cash_commitment_pct,
        owned_share_exposure_pct=exposure.owned_share_exposure_pct,
        covered_call_encumbered_shares=exposure.covered_call_encumbered_shares,
    )


class ControlLoopAlertView(BaseModel):
    alert_id: str
    scope: str
    alert_type: str
    severity: str
    reason: str
    created_at: datetime
    resolved: bool
    resolved_at: datetime | None


def build_control_loop_alert_view(alert: ControlLoopAlert) -> ControlLoopAlertView:
    return ControlLoopAlertView(
        alert_id=alert.alert_id, scope=alert.scope, alert_type=alert.alert_type.value,
        severity=alert.severity.value, reason=alert.reason, created_at=alert.created_at,
        resolved=alert.resolved, resolved_at=alert.resolved_at,
    )
