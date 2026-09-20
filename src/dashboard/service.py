"""Dashboard service layer (Step 18): the human-execution workflow on
top of an already-approved `FidelityTradeTicket`. Every action here is
one of the five Step 18 explicitly allows — REFRESH PRICE, VIEW ANALYSIS
(a pure read, needs no function here), COPY FIDELITY ORDER, MARK ORDER
ENTERED, REJECT TRADE — plus the FILL/PARTIALLY_FILLED/CANCELLED
outcomes and the REPRICE flow. **There is no function anywhere in this
module, or in anything it imports, that submits an order to Fidelity.**
Every state change is either (a) a read of already-computed data, (b) a
`src.brokers.fidelity.transition`/`confirm_fill` state-machine step, or
(c) a re-run of the existing, deterministic Quant Engine
(`src.orchestration.pipeline.default_quant_stage`) and Risk Engine
(`src.risk.engine.evaluate_trade_proposal`) — never a new, parallel
implementation of either.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from src.brokers.fidelity import (
    ExecutionConfirmation,
    FidelityTradeTicket,
    InvalidTransitionError,
    TicketStatus,
    confirm_fill,
    transition,
)
from src.data.option_chain import OptionChain
from src.data.provider import StaleDataError
from src.llm.devils_advocate import DevilsAdvocateEvaluation
from src.llm.schemas import TradeProposal
from src.orchestration.pipeline import default_quant_stage
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import RiskDecisionResult, evaluate_trade_proposal
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import TradeRiskError
from src.workflows.morning_scan import MorningScanReport

from src.dashboard.models import (
    AuditEvent,
    AuditEventType,
    AuditLog,
    DashboardState,
    OpportunityRecord,
    OrderEntryRecord,
)
from src.dashboard.ticket_format import render_dashboard_order_text

# Reuses the exact threshold `FidelityTradeTicket`'s own validator
# enforces at construction time ("a ticket must never be generated from
# stale market data") -- one canonical freshness policy, not a second,
# dashboard-specific number that could quietly drift from it.
from src.brokers.fidelity import MAX_MARKET_DATA_AGE

_STALE_REFRESHABLE_STATUSES = frozenset({TicketStatus.AWAITING_HUMAN, TicketStatus.ORDER_ENTERED})
_REFRESH_ELIGIBLE_STATUSES = frozenset({TicketStatus.AWAITING_HUMAN, TicketStatus.REPRICE_REQUIRED})


class DashboardActionError(ValueError):
    """Every user-facing rejection this module raises — an action
    attempted from the wrong ticket status, invalid input, or the like.
    The FastAPI layer (`src.dashboard.app`) turns every one of these
    into a 400 response; nothing here ever silently no-ops."""


class OpportunityNotFoundError(DashboardActionError):
    """A bad/unknown trade_id — mapped to 404, not 400, by the FastAPI
    layer."""


def _require_opportunity(state: DashboardState, trade_id: str) -> OpportunityRecord:
    record = state.opportunities.get(trade_id)
    if record is None:
        raise OpportunityNotFoundError(f"no tracked opportunity for trade_id={trade_id!r}")
    return record


def get_opportunity(state: DashboardState, trade_id: str) -> OpportunityRecord:
    """Public read accessor — a plain lookup, no side effects, no
    staleness check (see `check_and_apply_staleness` for that)."""
    return _require_opportunity(state, trade_id)


def _audit(state: DashboardState, *, trade_id: str | None, event_type: AuditEventType, actor: str, detail: str, at: datetime) -> None:
    state.audit_log.record(
        AuditEvent(event_id=str(uuid.uuid4()), trade_id=trade_id, event_type=event_type, at=at, actor=actor, detail=detail)
    )


# --------------------------------------------------------------- setup


def build_state_from_morning_scan(
    report: MorningScanReport, *, portfolio: Portfolio, limits, now: datetime, actor: str = "system",
    portfolio_net_delta: float | None = None, portfolio_net_theta: float | None = None,
    portfolio_net_vega: float | None = None, daily_pnl: float | None = None, ytd_return_pct: float | None = None,
) -> DashboardState:
    """Projects a `/morning-scan` run's results into dashboard
    opportunities — only the candidates that actually reached a
    Risk-Engine-generated `FidelityTradeTicket` are shown; everything
    else already has its own rejection reason in the scan report and is
    not something a human can act on here."""
    state = DashboardState(
        portfolio=portfolio, limits=limits, portfolio_net_delta=portfolio_net_delta,
        portfolio_net_theta=portfolio_net_theta, portfolio_net_vega=portfolio_net_vega,
        daily_pnl=daily_pnl, ytd_return_pct=ytd_return_pct,
    )
    for result in report.results:
        outcome = result.outcome
        ticket = outcome.fidelity_ticket
        if ticket is None:
            continue
        record = OpportunityRecord(
            trade_id=ticket.trade_id,
            proposal=result.candidate.proposal,
            market_data=None,  # the scan doesn't retain each candidate's chain past building the ticket
            quantitative_analysis=outcome.quantitative_analysis,
            devils_advocate_review=outcome.devils_advocate_review,
            risk_decision=outcome.risk_decision,
            ticket=ticket,
        )
        state.opportunities[ticket.trade_id] = record
        _audit(state, trade_id=ticket.trade_id, event_type=AuditEventType.APPROVAL, actor=actor, at=now,
               detail=f"Risk Engine {outcome.risk_decision.decision.value if outcome.risk_decision else 'approved'}: ticket awaiting human execution")
    return state


def register_opportunity(
    state: DashboardState, *, proposal: TradeProposal, market_data: OptionChain,
    quantitative_analysis, devils_advocate_review: DevilsAdvocateEvaluation | None,
    risk_decision: RiskDecisionResult, now: datetime, actor: str = "system",
) -> OpportunityRecord:
    """Direct registration path (used by tests and by any caller that
    already has a full pipeline outcome in hand, rather than a whole
    `MorningScanReport`). Requires a ticket to already exist on
    `risk_decision` -- this module never builds one itself."""
    if risk_decision.fidelity_ticket is None:
        raise DashboardActionError("risk_decision carries no fidelity_ticket to track")
    ticket = risk_decision.fidelity_ticket
    record = OpportunityRecord(
        trade_id=ticket.trade_id, proposal=proposal, market_data=market_data,
        quantitative_analysis=quantitative_analysis, devils_advocate_review=devils_advocate_review,
        risk_decision=risk_decision, ticket=ticket,
    )
    state.opportunities[ticket.trade_id] = record
    _audit(state, trade_id=ticket.trade_id, event_type=AuditEventType.APPROVAL, actor=actor, at=now,
           detail=f"Risk Engine {risk_decision.decision.value}: ticket awaiting human execution")
    return record


# ------------------------------------------------------- staleness/reprice


def check_and_apply_staleness(state: DashboardState, trade_id: str, now: datetime, *, actor: str = "system") -> OpportunityRecord:
    """View-time enforcement of Step 18's REPRICE rule: "if market data
    exceeds configured freshness, disable COPY FIDELITY ORDER / display
    REPRICE REQUIRED." Called on every read of an opportunity (not just
    after an explicit REFRESH PRICE click) so the dashboard never shows
    a ticket as actionable once its quote has gone stale, purely by the
    clock moving forward with no action taken."""
    record = _require_opportunity(state, trade_id)
    if record.ticket.status not in _STALE_REFRESHABLE_STATUSES:
        return record
    age = now - record.ticket.market_data_timestamp
    if age > MAX_MARKET_DATA_AGE:
        record.ticket = transition(record.ticket, TicketStatus.REPRICE_REQUIRED, at=now)
        _audit(state, trade_id=trade_id, event_type=AuditEventType.REFRESH, actor=actor, at=now,
               detail=f"market data {age} old, exceeds {MAX_MARKET_DATA_AGE} — REPRICE REQUIRED")
    return record


def refresh_price(
    state: DashboardState, trade_id: str, *, fresh_market_data: OptionChain, now: datetime,
    manual_broker_capabilities: BrokerCapabilities, actor: str,
) -> OpportunityRecord:
    """REFRESH PRICE: refresh underlying/option quotes/Greeks/spread
    value (`fresh_market_data`, supplied by the caller — this module has
    no market-data connection of its own, same boundary every other
    workflow in this codebase holds to), then rerun the Quant Engine and
    Risk Engine before re-enabling the ticket. Only callable while the
    ticket is AWAITING_HUMAN or REPRICE_REQUIRED — once a human has
    entered the order into Fidelity, this workflow no longer applies to
    it (see `check_and_apply_staleness` for the passive ORDER_ENTERED ->
    REPRICE_REQUIRED path instead)."""
    record = _require_opportunity(state, trade_id)
    if record.ticket.status not in _REFRESH_ELIGIBLE_STATUSES:
        raise DashboardActionError(
            f"cannot refresh a ticket at status {record.ticket.status.value!r} — only "
            f"{sorted(s.value for s in _REFRESH_ELIGIBLE_STATUSES)} are refreshable"
        )

    try:
        qa = default_quant_stage(record.proposal, fresh_market_data, state.portfolio, state.limits, now=now)
    except (TradeRiskError, StaleDataError) as exc:
        if record.ticket.status != TicketStatus.REPRICE_REQUIRED:
            record.ticket = transition(record.ticket, TicketStatus.REPRICE_REQUIRED, at=now)
        _audit(state, trade_id=trade_id, event_type=AuditEventType.REFRESH, actor=actor, at=now,
               detail=f"refresh could not resolve fresh economics, still stale: {exc}")
        return record

    risk_result = evaluate_trade_proposal(
        record.proposal, state.portfolio, qa, fresh_market_data, manual_broker_capabilities, limits=state.limits, now=now,
    )
    record.market_data = fresh_market_data
    record.quantitative_analysis = qa
    record.risk_decision = risk_result
    _audit(state, trade_id=trade_id, event_type=AuditEventType.REFRESH, actor=actor, at=now,
           detail=f"quant/risk re-run: {risk_result.decision.value} — {risk_result.message}")

    if risk_result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE) and risk_result.fidelity_ticket is not None:
        record.ticket = risk_result.fidelity_ticket  # freshly generated -> status is already AWAITING_HUMAN
        _audit(state, trade_id=trade_id, event_type=AuditEventType.APPROVAL, actor="risk_engine", at=now,
               detail=f"re-approved after refresh: {risk_result.message}")
    else:
        # The deterministic Risk Engine's veto is absolute, even on a
        # refresh: it no longer approves this trade at the new price,
        # so the ticket is rejected outright rather than left showing a
        # stale, no-longer-valid approval.
        record.ticket = transition(record.ticket, TicketStatus.REJECTED, at=now)
        record.rejection_reason = risk_result.message
        _audit(state, trade_id=trade_id, event_type=AuditEventType.REJECTION, actor="risk_engine", at=now,
               detail=f"Risk Engine no longer approves after refresh: {risk_result.message}")
    return record


# ------------------------------------------------------------- ticket copy


def copy_fidelity_order(state: DashboardState, trade_id: str, now: datetime, *, actor: str) -> str:
    """COPY FIDELITY ORDER. Disabled (raises) whenever the ticket is
    stale/REPRICE_REQUIRED or otherwise not currently actionable —
    `check_and_apply_staleness` is applied first so a caller can never
    copy a ticket that has quietly gone stale since it was last shown."""
    record = check_and_apply_staleness(state, trade_id, now, actor=actor)
    if record.ticket.status != TicketStatus.AWAITING_HUMAN:
        raise DashboardActionError(
            f"COPY FIDELITY ORDER is disabled for a ticket at status {record.ticket.status.value!r} "
            "(REPRICE REQUIRED, or otherwise not currently actionable)"
        )
    text = render_dashboard_order_text(record.ticket)
    _audit(state, trade_id=trade_id, event_type=AuditEventType.TICKET_COPY, actor=actor, at=now, detail="ticket text copied")
    return text


# ------------------------------------------------------------- order entry


def mark_order_entered(
    state: DashboardState, trade_id: str, *, actual_limit_entered: float, contracts: int,
    entered_at: datetime, entered_by: str,
) -> OpportunityRecord:
    """MARK ORDER ENTERED. Requires the three fields Step 18 names:
    the actual Fidelity limit entered, contracts, and a timestamp — all
    supplied by the human, since this module has no way to know what
    they actually typed into Fidelity Trader+."""
    record = _require_opportunity(state, trade_id)
    if record.ticket.status != TicketStatus.AWAITING_HUMAN:
        raise DashboardActionError(
            f"cannot mark order entered from status {record.ticket.status.value!r} — the ticket must be "
            "AWAITING_HUMAN (not stale, not already entered, not rejected)"
        )
    if contracts <= 0:
        raise DashboardActionError("contracts must be positive")
    if actual_limit_entered <= 0:
        raise DashboardActionError("actual_limit_entered must be positive")

    record.ticket = transition(record.ticket, TicketStatus.ORDER_ENTERED, at=entered_at)
    record.order_entry = OrderEntryRecord(
        actual_limit_entered=actual_limit_entered, contracts=contracts, entered_at=entered_at, entered_by=entered_by,
    )
    _audit(
        state, trade_id=trade_id, event_type=AuditEventType.ORDER_ENTERED, actor=entered_by, at=entered_at,
        detail=f"entered {contracts} contract(s) at limit ${actual_limit_entered:.2f}",
    )
    return record


# ------------------------------------------------------------------- fills


def _scaled_position_from_fill(record: OpportunityRecord, confirmation: ExecutionConfirmation) -> PortfolioPosition:
    ticket = record.ticket
    ratio = confirmation.filled_quantity / ticket.quantity
    legs = [
        PortfolioPositionLeg(
            right=leg.put_call.value,
            side="buy" if leg.action.value.startswith("buy") else "sell",
            strike=leg.strike,
            entry_price=abs(confirmation.fill_price),
        )
        for leg in ticket.legs
    ]
    return PortfolioPosition(
        position_id=ticket.trade_id,
        ticker=ticket.ticker,
        sector="UNKNOWN",  # overwritten by the caller from Portfolio.sector_by_ticker when available
        strategy=record.proposal.strategy,
        expiration=ticket.expiration,
        legs=legs,
        contracts=confirmation.filled_quantity,
        capital_at_risk=ticket.capital_at_risk * ratio,
        max_loss=ticket.max_loss * ratio,
        opened_at=confirmation.confirmed_at,
    )


def _apply_confirmed_fill_to_portfolio(portfolio: Portfolio, record: OpportunityRecord, confirmation: ExecutionConfirmation) -> Portfolio:
    """"Update the portfolio only after fill confirmation" (Step 18) —
    mirrors `src.orchestration.pipeline.default_portfolio_update_stage`'s
    own cash/capital_at_risk bookkeeping (see its SY-005 fix docstring
    for why `cash` is reduced by exactly the new position's
    `capital_at_risk`), applied here to a confirmed Fidelity fill
    instead of a PaperBroker fill."""
    new_position = _scaled_position_from_fill(record, confirmation)
    new_position = new_position.model_copy(update={"sector": portfolio.sector_by_ticker.get(new_position.ticker, "UNKNOWN")})
    new_cash = portfolio.cash - new_position.capital_at_risk
    if new_cash < 0:
        raise DashboardActionError(
            f"confirmed fill would drive portfolio cash negative ({new_cash:.2f}): "
            f"capital_at_risk={new_position.capital_at_risk:.2f} exceeds available cash={portfolio.cash:.2f}"
        )
    return portfolio.model_copy(update={"positions": [*portfolio.positions, new_position], "cash": new_cash})


def record_fill(
    state: DashboardState, trade_id: str, *, status: str, fill_price: float, contracts_filled: int,
    confirmed_at: datetime, confirmed_by: str,
) -> OpportunityRecord:
    """FILLED or PARTIALLY_FILLED. Requires actual fill price, contracts
    filled, and a timestamp (Step 18) — builds a real
    `ExecutionConfirmation` and moves the ticket through
    `confirm_fill()`, the one function in this codebase that can reach
    either status. The portfolio is updated only here, never earlier.

    `status` is the caller's declared intent (which of the two buttons
    was clicked); the *actual* resulting status always comes from
    `confirm_fill()`'s own arithmetic (`filled_quantity >= ticket
    .quantity`), never from this parameter — a caller mislabeling a
    partial fill as FILLED (or vice versa) still gets the numerically
    correct outcome, not whatever they clicked."""
    if status not in ("FILLED", "PARTIALLY_FILLED"):
        raise DashboardActionError(f"status must be FILLED or PARTIALLY_FILLED, got {status!r}")
    record = _require_opportunity(state, trade_id)
    confirmation = ExecutionConfirmation(
        confirmed_by=confirmed_by, confirmation_source="human_manual_entry",
        filled_quantity=contracts_filled, fill_price=fill_price, confirmed_at=confirmed_at,
    )
    try:
        record.ticket = confirm_fill(record.ticket, confirmation)
    except InvalidTransitionError as exc:
        raise DashboardActionError(str(exc)) from exc

    # A caller-declared PARTIALLY_FILLED that actually fills the full
    # quantity is resolved by confirm_fill() itself to FILLED (its own
    # `filled_quantity >= ticket.quantity` rule) -- trust its verdict,
    # not the caller's requested label.
    state.portfolio = _apply_confirmed_fill_to_portfolio(state.portfolio, record, confirmation)

    event_type = AuditEventType.FILL if record.ticket.status == TicketStatus.FILLED else AuditEventType.PARTIAL_FILL
    _audit(
        state, trade_id=trade_id, event_type=event_type, actor=confirmed_by, at=confirmed_at,
        detail=f"{record.ticket.status.value}: {contracts_filled} contract(s) at ${fill_price:.2f}",
    )
    return record


def cancel_order(state: DashboardState, trade_id: str, *, reason: str, at: datetime, actor: str) -> OpportunityRecord:
    """CANCELLED — for an order that was already entered into Fidelity
    and is now being pulled back (see REJECTED for the pre-entry
    decline)."""
    record = _require_opportunity(state, trade_id)
    try:
        record.ticket = transition(record.ticket, TicketStatus.CANCELLED, at=at)
    except InvalidTransitionError as exc:
        raise DashboardActionError(str(exc)) from exc
    record.cancellation_reason = reason
    _audit(state, trade_id=trade_id, event_type=AuditEventType.CANCELLATION, actor=actor, at=at, detail=reason)
    return record


def reject_trade(state: DashboardState, trade_id: str, *, reason: str, at: datetime, actor: str) -> OpportunityRecord:
    """REJECT TRADE — a human declining an already-approved ticket
    before ever entering it into Fidelity. Distinct from CANCELLED
    (an order that was entered, then pulled back)."""
    record = _require_opportunity(state, trade_id)
    try:
        record.ticket = transition(record.ticket, TicketStatus.REJECTED, at=at)
    except InvalidTransitionError as exc:
        raise DashboardActionError(str(exc)) from exc
    record.rejection_reason = reason
    _audit(state, trade_id=trade_id, event_type=AuditEventType.REJECTION, actor=actor, at=at, detail=reason)
    return record
