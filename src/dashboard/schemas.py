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

from pydantic import BaseModel, ConfigDict, Field

from src.brokers.fidelity import FidelityLegAction, TicketStatus
from src.data.option_chain import OptionRight
from src.llm.schemas import DevilsAdvocateReview
from src.risk.reason_codes import ReasonCode, RiskDecision


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
