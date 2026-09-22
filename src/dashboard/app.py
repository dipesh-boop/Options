"""The Fidelity human-execution dashboard's FastAPI application (Step 18).

**This application never submits a securities/options order to
Fidelity, and never can.** Every route below is read-only, or one of
the five explicitly-allowed human actions (REFRESH PRICE, COPY FIDELITY
ORDER, MARK ORDER ENTERED, recording a FILLED/PARTIALLY_FILLED/
CANCELLED outcome, REJECT TRADE) — implemented entirely on top of
`src.dashboard.service`, which is itself built entirely on
`src.brokers.fidelity`'s transition/confirm_fill state machine and the
existing, deterministic Quant/Risk Engines. There is no route here
named (or shaped like) AUTO TRADE, EXECUTE, or SEND TO FIDELITY, no
route that accepts a broker order-submission payload, and no import
anywhere in this module of a network client capable of reaching
Fidelity. See `tests/unit/dashboard/test_app_security.py` for the
structural proof, including a route-inventory test that fails if any
future change adds an execution-shaped endpoint.

**Security:** no route, request model, or response model anywhere in
this package has a field for a Fidelity username, password, MFA code,
or session cookie — this dashboard identifies a human action only by a
free-text `actor`/`confirmed_by`/`entered_by` name (an audit-trail
label, not a credential), never an authentication secret of any kind.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.brokers.order_validator import build_occ_symbol
from src.data.option_chain import OptionChain, OptionContract
from src.data.quotes import UnderlyingQuote
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.reason_codes import RiskDecision

from src.dashboard import schemas, service
from src.dashboard.models import DashboardState
from src.dashboard.risk_state import build_risk_panel
from src.dashboard.service import DashboardActionError, OpportunityNotFoundError

app = FastAPI(title="Fidelity Human-Execution Dashboard", version="1.0.0")

_STATIC_DIR = __file__.rsplit("/", 1)[0] + "/static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")

# Manual-execution capability the dashboard re-runs the Risk Engine
# against on every REFRESH PRICE -- identical to what `/morning-scan`
# already uses to produce the very ticket this dashboard displays.
_MANUAL_CAPABILITIES = BrokerCapabilities(
    broker_name="fidelity", execution_mode="MANUAL", account_alias="OPTIONS_ACCOUNT",
    options_enabled=True, allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD"],
)

# The whole dashboard session's state. A local, single-user dashboard
# has exactly one of these; tests override this dependency to inject a
# fixture-built state without touching this module-level default.
_dashboard_state: DashboardState | None = None


def get_state() -> DashboardState:
    if _dashboard_state is None:
        raise HTTPException(status_code=503, detail="dashboard has no loaded portfolio/opportunities yet")
    return _dashboard_state


def set_state(state: DashboardState) -> None:
    """Called by whatever loads a `/morning-scan` run (or a test) into
    this process — the only place `_dashboard_state` is ever assigned."""
    global _dashboard_state
    _dashboard_state = state


def get_now() -> datetime:
    return datetime.now(timezone.utc)


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DashboardActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------- read views


@app.get("/api/portfolio-header", response_model=schemas.PortfolioHeaderView)
def portfolio_header(state: DashboardState = Depends(get_state)) -> schemas.PortfolioHeaderView:
    return schemas.build_portfolio_header_view(state)


@app.get("/api/risk-panel", response_model=schemas.RiskPanelResponseView)
def risk_panel(state: DashboardState = Depends(get_state)) -> schemas.RiskPanelResponseView:
    return schemas.build_risk_panel_view(build_risk_panel(state.portfolio, state.limits))


@app.get("/api/data-provider-health", response_model=schemas.DataProviderHealthView)
async def data_provider_health() -> schemas.DataProviderHealthView:
    """Read-only (Step 22.1, Part 16): reports which market-data
    provider is configured, whether it's actually connected, and
    whether options data is OPRA or indicative/delayed -- distinct from
    `get_state`'s loaded-portfolio dependency, since this reflects
    config, not a loaded `/morning-scan` run. Never accepts input, and
    never touches Fidelity/PaperBroker/order-submission of any kind."""
    from src.data.provider_health import check_provider_health

    report = await check_provider_health(now=get_now())
    return schemas.build_data_provider_health_view(report)


@app.get("/api/wheels", response_model=list[schemas.WheelView])
def list_wheels(state: DashboardState = Depends(get_state), now: datetime = Depends(get_now)) -> list[schemas.WheelView]:
    """Step 22.2, Part 19: read-only Wheel visibility, clearly labeled
    RESEARCH / PAPER by construction -- this route only ever reads
    `state.wheels` (populated from `src.wheel.persistence`, never from
    anything Fidelity-shaped) and exposes no action of any kind. There is
    no POST/PUT route anywhere for a Wheel -- opening/closing a Wheel's
    CSP/CC legs happens through the ordinary Risk-Engine-gated
    PaperBroker/Fidelity paths (`src.wheel.paper_events`/`fidelity_events`),
    never through this dashboard."""
    return [
        schemas.build_wheel_view(w, current_underlying_price=state.current_price_by_ticker.get(w.ticker), now=now)
        for w in state.wheels.values()
    ]


@app.get("/api/wheels/{wheel_id}", response_model=schemas.WheelView)
def get_wheel(wheel_id: str, state: DashboardState = Depends(get_state), now: datetime = Depends(get_now)) -> schemas.WheelView:
    wheel = state.wheels.get(wheel_id)
    if wheel is None:
        raise HTTPException(status_code=404, detail=f"no Wheel found for wheel_id={wheel_id!r}")
    return schemas.build_wheel_view(wheel, current_underlying_price=state.current_price_by_ticker.get(wheel.ticker), now=now)


@app.get("/api/lifecycle", response_model=list[schemas.LifecyclePositionView])
def list_lifecycle_positions(state: DashboardState = Depends(get_state)) -> list[schemas.LifecyclePositionView]:
    """Step 22.3, Part 22: read-only Active Positions/Lifecycle
    visibility. This route only ever reads `state.lifecycle_positions`
    (populated from `src.lifecycle.persistence`) and exposes no action
    of any kind -- there is no POST/PUT route anywhere for a lifecycle
    position. A close/roll/adjustment happens through the ordinary
    Risk-Engine-gated PaperBroker/Fidelity paths
    (`src.lifecycle.paper_events`/`fidelity_events`), never through
    this dashboard."""
    return [
        schemas.build_lifecycle_position_view(
            record,
            latest_snapshot=state.lifecycle_latest_snapshot.get(trade_id),
            resolved=state.lifecycle_latest_resolved.get(trade_id),
            entry_dte=None,
            entry_premium=None,
            next_scheduled_review_dte=None,
        )
        for trade_id, record in state.lifecycle_positions.items()
    ]


@app.get("/api/lifecycle/{trade_id}", response_model=schemas.LifecyclePositionView)
def get_lifecycle_position(trade_id: str, state: DashboardState = Depends(get_state)) -> schemas.LifecyclePositionView:
    record = state.lifecycle_positions.get(trade_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no lifecycle position found for trade_id={trade_id!r}")
    return schemas.build_lifecycle_position_view(
        record,
        latest_snapshot=state.lifecycle_latest_snapshot.get(trade_id),
        resolved=state.lifecycle_latest_resolved.get(trade_id),
        entry_dte=None,
        entry_premium=None,
        next_scheduled_review_dte=None,
    )


@app.get("/api/opportunities", response_model=list[schemas.OpportunityView])
def list_opportunities(
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> list[schemas.OpportunityView]:
    views = []
    for trade_id in state.opportunities:
        record = _handle(service.check_and_apply_staleness, state, trade_id, now)
        views.append(schemas.build_opportunity_view(record, now))
    return views


@app.get("/api/opportunities/{trade_id}", response_model=schemas.OpportunityView)
def get_opportunity(
    trade_id: str, state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    """VIEW ANALYSIS: a pure read of everything already computed for
    this candidate — no audit event is recorded for a view, per Step
    18's own audit list (refresh/approval/rejection/copy/order-entered/
    fill/partial-fill/cancellation — a view is not among them)."""
    record = _handle(service.check_and_apply_staleness, state, trade_id, now)
    return schemas.build_opportunity_view(record, now)


@app.get("/api/control-loop/status", response_model=schemas.ControlCycleStatusView)
def control_loop_status(state: DashboardState = Depends(get_state)) -> schemas.ControlCycleStatusView:
    """Part 33's SYSTEM STATUS/MARKET DATA sections, read-only. 404s
    honestly (never a fabricated "idle"/zeroed record) when the control
    loop hasn't run yet against this dashboard session."""
    if state.latest_cycle_record is None:
        raise HTTPException(status_code=404, detail="no control-loop cycle has run yet")
    return schemas.build_control_cycle_status_view(state.latest_cycle_record)


@app.get("/api/control-loop/exposure", response_model=schemas.PortfolioExposureView)
def control_loop_exposure(state: DashboardState = Depends(get_state)) -> schemas.PortfolioExposureView:
    """Part 15's exposure snapshot, read-only -- the same object
    `src.portfolio.exposure.build_exposure_snapshot` produced for the
    most recent control-loop cycle, never recomputed here."""
    if state.latest_exposure is None:
        raise HTTPException(status_code=404, detail="no portfolio exposure snapshot available yet")
    return schemas.build_exposure_view(state.latest_exposure)


@app.get("/api/control-loop/alerts", response_model=list[schemas.ControlLoopAlertView])
def control_loop_alerts(
    state: DashboardState = Depends(get_state), unresolved_only: bool = True,
) -> list[schemas.ControlLoopAlertView]:
    """Part 32's alert feed, read-only. Sorted CRITICAL-first, then
    oldest-first within a severity (the longest-outstanding condition of
    a given severity surfaces first), matching Part 33's Action Center
    ordering ("never put new trade above required Risk action") --
    display ordering only, no route here can resolve or dismiss an
    alert (that happens wherever the control loop itself re-evaluates
    the underlying condition, not through this dashboard)."""
    severity_rank = {"critical": 0, "warning": 1, "review": 2, "info": 3}
    alerts = list(state.control_loop_alerts.values())
    if unresolved_only:
        alerts = [a for a in alerts if not a.resolved]
    alerts.sort(key=lambda a: (severity_rank.get(a.severity.value, 99), a.created_at), reverse=False)
    return [schemas.build_control_loop_alert_view(a) for a in alerts]


@app.get("/api/audit", response_model=list[schemas.AuditEventView])
def audit_log(state: DashboardState = Depends(get_state)) -> list[schemas.AuditEventView]:
    return [schemas.build_audit_event_view(e) for e in state.audit_log.all()]


@app.get("/api/audit/{trade_id}", response_model=list[schemas.AuditEventView])
def audit_log_for_trade(trade_id: str, state: DashboardState = Depends(get_state)) -> list[schemas.AuditEventView]:
    return [schemas.build_audit_event_view(e) for e in state.audit_log.for_trade(trade_id)]


# ---------------------------------------------------------------- actions


@app.post("/api/opportunities/{trade_id}/refresh", response_model=schemas.OpportunityView)
def refresh_price(
    trade_id: str, request: schemas.RefreshPriceRequest,
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    record = _handle(service.get_opportunity, state, trade_id)
    ticket = record.ticket
    contracts = [
        OptionContract(
            underlying=ticket.ticker,
            option_symbol=build_occ_symbol(ticket.ticker, ticket.expiration, leg.put_call, leg.strike),
            expiration=ticket.expiration, strike=leg.strike, right=leg.put_call,
            bid=q.bid, ask=q.ask, last=(q.bid + q.ask) / 2, volume=q.volume, open_interest=q.open_interest, iv=q.iv,
            underlying_price=request.underlying_price, timestamp=request.quote_timestamp, source="dashboard_refresh",
        )
        for leg in ticket.legs
        for q in request.leg_quotes
        if q.strike == leg.strike and q.right == leg.put_call
    ]
    fresh_market_data = OptionChain(
        underlying=UnderlyingQuote(
            symbol=ticket.ticker, bid=request.underlying_bid, ask=request.underlying_ask,
            last=request.underlying_price, volume=0, timestamp=request.quote_timestamp, source="dashboard_refresh",
        ),
        contracts=contracts, timestamp=request.quote_timestamp, source="dashboard_refresh",
    )
    record = _handle(
        service.refresh_price, state, trade_id, fresh_market_data=fresh_market_data, now=now,
        manual_broker_capabilities=_MANUAL_CAPABILITIES, actor="dashboard_user",
    )
    return schemas.build_opportunity_view(record, now)


@app.post("/api/opportunities/{trade_id}/copy")
def copy_fidelity_order(
    trade_id: str, state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> dict[str, str]:
    text = _handle(service.copy_fidelity_order, state, trade_id, now, actor="dashboard_user")
    return {"text": text}


@app.post("/api/opportunities/{trade_id}/mark-order-entered", response_model=schemas.OpportunityView)
def mark_order_entered(
    trade_id: str, request: schemas.MarkOrderEnteredRequest,
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    record = _handle(
        service.mark_order_entered, state, trade_id, actual_limit_entered=request.actual_limit_entered,
        contracts=request.contracts, entered_at=request.entered_at or now, entered_by=request.entered_by,
    )
    return schemas.build_opportunity_view(record, now)


@app.post("/api/opportunities/{trade_id}/fill", response_model=schemas.OpportunityView)
def record_fill(
    trade_id: str, request: schemas.RecordFillRequest,
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    record = _handle(
        service.record_fill, state, trade_id, status=request.status, fill_price=request.fill_price,
        contracts_filled=request.contracts_filled, confirmed_at=request.confirmed_at or now, confirmed_by=request.confirmed_by,
    )
    return schemas.build_opportunity_view(record, now)


@app.post("/api/opportunities/{trade_id}/cancel", response_model=schemas.OpportunityView)
def cancel_order(
    trade_id: str, request: schemas.CancelOrderRequest,
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    record = _handle(service.cancel_order, state, trade_id, reason=request.reason, at=now, actor=request.actor)
    return schemas.build_opportunity_view(record, now)


@app.post("/api/opportunities/{trade_id}/reject", response_model=schemas.OpportunityView)
def reject_trade(
    trade_id: str, request: schemas.RejectTradeRequest,
    state: DashboardState = Depends(get_state), now: datetime = Depends(get_now),
) -> schemas.OpportunityView:
    record = _handle(service.reject_trade, state, trade_id, reason=request.reason, at=now, actor=request.actor)
    return schemas.build_opportunity_view(record, now)
