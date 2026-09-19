"""The required order pipeline (Step 12):

    TradeProposal
      -> Quant Engine
      -> Devil's Advocate
      -> Portfolio Manager
      -> Risk Engine
      -> Order Validator
      -> PaperBroker
      -> Simulated Fill
      -> Portfolio
      -> Database

Every stage above the input itself is a required, explicitly supplied
dependency on `PipelineStages`. `run_order_pipeline` checks every one of
them is present *before* touching any of them — a missing stage rejects
the order (`PipelineStatus.REJECTED`, `rejected_stage` naming which one)
rather than silently skipping it or falling back to a default behavior.

This module is the first thing in the codebase that actually calls
`src.llm.devils_advocate`, `src.llm.portfolio_manager`, and
`src.risk.engine` back-to-back against one shared scenario — every
prior step built and tested these stages in isolation; this is where
they connect. `FidelityTradeTicket` is generated "at the same time" as
the paper fill (Step 12's own wording) by running the same Risk Engine
stage a second time against a MANUAL broker capability — never
constructed by hand, always the Risk Engine's own, independently
computed artifact.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from src.brokers.base import Order, PlaceOrderRequest
from src.brokers.fidelity import FidelityTradeTicket
from src.brokers.order_validator import OrderValidationError
from src.brokers.paper import PaperBroker
from src.data.option_chain import OptionChain
from src.llm.context import MarketSnapshotContext, PortfolioStateContext, QuantitativeAnalysisContext, RiskEngineContext
from src.llm.devils_advocate import DevilsAdvocateEvaluation, DevilsAdvocateInputs
from src.llm.devils_advocate import MissingInputError as DevilsAdvocateMissingInputError
from src.llm.portfolio_manager import PortfolioManagerEvaluation, PortfolioManagerInputs
from src.llm.portfolio_manager import MissingInputError as PortfolioManagerMissingInputError
from src.llm.schemas import AdversarialReview, MarketRegimeAssessment, RiskReviewNote, TradeProposal
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import RiskDecisionResult
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import QuantitativeAnalysis, compute_trade_economics, compute_trade_greeks, resolve_leg_contracts


class PipelineStatus(str, Enum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    NO_FILL = "no_fill"
    REJECTED = "rejected"
    REPRICE_REQUIRED = "reprice_required"


@dataclass(frozen=True)
class DatabaseRecord:
    """One append-only pipeline-run record. Phase 0's real, persisted
    database doesn't exist yet (flagged since the project's very first
    progress.md entry); this in-memory placeholder follows the same
    append-only pattern as `InMemoryIdempotencyStore`/`InMemoryAuditLog`
    rather than inventing a new one."""

    record_id: str
    proposal_id: str
    status: PipelineStatus
    order: Order | None
    fidelity_ticket: FidelityTradeTicket | None
    created_at: datetime


class Database:
    """Minimal append-only interface — a real implementation is a Phase
    0 item; `InMemoryDatabase` below is the only implementation today."""

    def save(self, record: DatabaseRecord) -> None:
        raise NotImplementedError

    def all(self) -> list[DatabaseRecord]:
        raise NotImplementedError


class InMemoryDatabase(Database):
    def __init__(self) -> None:
        self._records: list[DatabaseRecord] = []

    def save(self, record: DatabaseRecord) -> None:
        self._records.append(record)

    def all(self) -> list[DatabaseRecord]:
        return list(self._records)


def default_quant_stage(
    proposal: TradeProposal, market_data: OptionChain, portfolio: Portfolio, limits: RiskLimitsConfig, *, now: datetime
) -> QuantitativeAnalysis:
    """The pipeline's Quant Engine stage: independently prices the
    proposal from current market data, reusing exactly the functions
    `src.risk.engine` itself uses (`resolve_leg_contracts`,
    `compute_trade_economics`, `compute_trade_greeks`) — never a second,
    parallel implementation of the same math."""
    contracts = resolve_leg_contracts(proposal, market_data, as_of=now, max_age_minutes=limits.max_market_data_age_minutes)
    economics = compute_trade_economics(proposal, contracts, portfolio, limits, num_contracts=proposal.contracts_requested)
    greeks = compute_trade_greeks(proposal, contracts, portfolio)
    return QuantitativeAnalysis(
        proposal_id=proposal.proposal_id,
        generated_at=now,
        max_profit=economics.max_profit,
        max_loss=economics.max_loss,
        breakeven=economics.breakeven,
        capital_required=economics.capital_required,
        return_on_capital=economics.return_on_capital,
        annualized_roc=economics.annualized_roc,
        probability_of_profit=economics.probability_of_profit,
        expected_value=economics.expected_value,
        net_delta=greeks.delta,
        net_vega=greeks.vega,
    )


def _context_from_quant(qa: QuantitativeAnalysis) -> QuantitativeAnalysisContext:
    return QuantitativeAnalysisContext(
        max_profit=qa.max_profit,
        max_loss=qa.max_loss,
        breakeven=qa.breakeven,
        capital_required=qa.capital_required,
        return_on_capital=qa.return_on_capital,
        annualized_roc=qa.annualized_roc,
        probability_of_profit=qa.probability_of_profit,
        expected_value=qa.expected_value,
    )


def _adversarial_review_from(review) -> AdversarialReview:
    """Bridges `src.llm.devils_advocate.DevilsAdvocateReview` (Step 11's
    richer, 18-category schema) into the lighter `AdversarialReview`
    `PortfolioManagerInputs` still expects (Step 10's original
    interface, kept unchanged) — an adapter, not a duplicate schema."""
    return AdversarialReview(
        proposal_id=review.proposal_id,
        critique=review.why_not_thesis,
        risk_flags=[],
        do_not_advance=review.verdict in ("REJECT", "REPRICE_REQUIRED"),
    )


def default_portfolio_update_stage(portfolio: Portfolio, proposal: TradeProposal, qa: QuantitativeAnalysis, order: Order) -> Portfolio:
    """The pipeline's Portfolio stage: folds a filled order into a new
    `Portfolio` snapshot (frozen, so this returns a new instance rather
    than mutating). Uses only what the fill itself proves — the order's
    own legs and fill quantity — never the originally requested size."""
    legs = [
        PortfolioPositionLeg(
            right=leg.right.value if leg.right is not None else "C",
            side="buy" if leg.action.value == "buy" else "sell",
            strike=leg.strike or 0.0,
            entry_price=abs(order.avg_fill_price or 0.0),
        )
        for leg in order.legs
        if leg.right is not None
    ]
    new_position = PortfolioPosition(
        position_id=order.broker_order_id or str(uuid.uuid4()),
        ticker=proposal.ticker,
        sector=portfolio.sector_by_ticker.get(proposal.ticker, "UNKNOWN"),
        strategy=proposal.strategy,
        expiration=proposal.expiration,
        legs=legs,
        contracts=order.filled_quantity,
        capital_at_risk=qa.max_loss * (order.filled_quantity / proposal.contracts_requested),
        max_loss=qa.max_loss * (order.filled_quantity / proposal.contracts_requested),
        opened_at=order.timestamp,
    )
    return portfolio.model_copy(update={"positions": [*portfolio.positions, new_position]})


@dataclass(frozen=True)
class PipelineStages:
    """Every required stage, as an explicit dependency. `None` on any
    field means that stage does not exist for this call — the pipeline
    rejects the order rather than proceed without it. There are no
    silently-injected defaults for any of these; `default_quant_stage`/
    `default_portfolio_update_stage`/`InMemoryDatabase` above exist to be
    passed in explicitly, not to be assumed."""

    quant_stage: Callable[[TradeProposal, OptionChain, Portfolio, RiskLimitsConfig], QuantitativeAnalysis] | None = None
    devils_advocate_stage: Callable[[DevilsAdvocateInputs], DevilsAdvocateEvaluation] | None = None
    portfolio_manager_stage: Callable[[PortfolioManagerInputs], PortfolioManagerEvaluation] | None = None
    risk_engine_stage: Callable[..., RiskDecisionResult] | None = None
    order_validator_stage: Callable[..., PlaceOrderRequest] | None = None
    paper_broker: PaperBroker | None = None
    portfolio_update_stage: Callable[[Portfolio, TradeProposal, QuantitativeAnalysis, Order], Portfolio] | None = None
    database: Database | None = None


_STAGE_FIELDS = (
    "quant_stage",
    "devils_advocate_stage",
    "portfolio_manager_stage",
    "risk_engine_stage",
    "order_validator_stage",
    "paper_broker",
    "portfolio_update_stage",
    "database",
)


@dataclass(frozen=True)
class PipelineRequest:
    proposal: TradeProposal
    market_data: OptionChain
    portfolio: Portfolio
    limits: RiskLimitsConfig
    market_regime: MarketRegimeAssessment
    risk_reviewer_note: RiskReviewNote
    analysis_snapshot: MarketSnapshotContext
    current_snapshot: MarketSnapshotContext
    automated_broker_capabilities: BrokerCapabilities | None
    manual_broker_capabilities: BrokerCapabilities | None
    now: datetime


@dataclass(frozen=True)
class PipelineOutcome:
    status: PipelineStatus
    rejected_stage: str | None
    reason: str
    quantitative_analysis: QuantitativeAnalysis | None = None
    devils_advocate_review: DevilsAdvocateEvaluation | None = None
    portfolio_manager_decision: PortfolioManagerEvaluation | None = None
    risk_decision: RiskDecisionResult | None = None
    order: Order | None = None
    fidelity_ticket: FidelityTradeTicket | None = None
    updated_portfolio: Portfolio | None = None


async def run_order_pipeline(request: PipelineRequest, stages: PipelineStages) -> PipelineOutcome:
    missing = [name for name in _STAGE_FIELDS if getattr(stages, name) is None]
    if missing:
        return _record(
            stages,
            PipelineOutcome(
                status=PipelineStatus.REJECTED,
                rejected_stage=missing[0],
                reason=f"required pipeline stage(s) not configured: {missing}",
            ),
            request,
        )

    # 1. Quant Engine
    try:
        qa = stages.quant_stage(request.proposal, request.market_data, request.portfolio, request.limits)
    except Exception as exc:  # noqa: BLE001 - any quant failure is a reject, not a crash
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "quant_engine", f"Quant Engine failed: {exc}"), request)

    qa_context = _context_from_quant(qa)
    portfolio_state = PortfolioStateContext(
        nav=request.portfolio.nav,
        net_delta=qa.net_delta,
        net_theta=0.0,
        net_vega=qa.net_vega,
        current_drawdown_pct=(request.portfolio.peak_equity - request.portfolio.nav) / request.portfolio.peak_equity,
        open_position_count=len(request.portfolio.positions),
    )

    # 2. Devil's Advocate — independent: no PortfolioDecision exists yet to leak to it.
    da_inputs = DevilsAdvocateInputs(
        proposal=request.proposal,
        quant_analysis=qa_context,
        portfolio_state=portfolio_state,
        market_regime=request.market_regime,
        analysis_snapshot=request.analysis_snapshot,
        current_snapshot=request.current_snapshot,
    )
    try:
        da_evaluation = stages.devils_advocate_stage(da_inputs)
    except DevilsAdvocateMissingInputError as exc:
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "devils_advocate", str(exc), quantitative_analysis=qa), request)
    except Exception as exc:  # noqa: BLE001
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "devils_advocate", f"Devil's Advocate failed: {exc}", quantitative_analysis=qa), request)

    if da_evaluation.review.verdict == "REPRICE_REQUIRED":
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REPRICE_REQUIRED, None, "Devil's Advocate flagged execution data as stale",
                quantitative_analysis=qa, devils_advocate_review=da_evaluation,
            ),
            request,
        )
    if da_evaluation.review.verdict == "REJECT":
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "devils_advocate", da_evaluation.review.why_not_thesis,
                quantitative_analysis=qa, devils_advocate_review=da_evaluation,
            ),
            request,
        )

    # 3. Portfolio Manager — sees the Devil's Advocate's completed
    # review, never a Risk Engine result (which hasn't run yet).
    pm_inputs = PortfolioManagerInputs(
        proposal=request.proposal,
        market_regime=request.market_regime,
        quant_analysis=qa_context,
        devil_advocate_review=_adversarial_review_from(da_evaluation.review),
        risk_reviewer_note=request.risk_reviewer_note,
        portfolio_state=portfolio_state,
        risk_engine_result=None,
    )
    try:
        pm_evaluation = stages.portfolio_manager_stage(pm_inputs)
    except PortfolioManagerMissingInputError as exc:
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "portfolio_manager", str(exc), quantitative_analysis=qa, devils_advocate_review=da_evaluation), request)
    except Exception as exc:  # noqa: BLE001
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "portfolio_manager", f"Portfolio Manager failed: {exc}", quantitative_analysis=qa, devils_advocate_review=da_evaluation), request)

    if pm_evaluation.decision.decision == "reject":
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "portfolio_manager", pm_evaluation.decision.thesis_summary,
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
            ),
            request,
        )
    if pm_evaluation.decision.decision == "hold_cash":
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.NO_FILL, None, "Portfolio Manager chose to hold cash",
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
            ),
            request,
        )

    # 4. Risk Engine — the sole authority; runs against the AUTOMATED
    # target (PaperBroker) first.
    try:
        risk_result: RiskDecisionResult = stages.risk_engine_stage(
            request.proposal, request.portfolio, qa, request.market_data, request.automated_broker_capabilities, limits=request.limits, now=request.now
        )
    except Exception as exc:  # noqa: BLE001
        return _record(stages, PipelineOutcome(PipelineStatus.REJECTED, "risk_engine", f"Risk Engine failed: {exc}", quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation), request)

    if risk_result.decision in (RiskDecision.REJECT, RiskDecision.HALT):
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "risk_engine", risk_result.message,
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation, risk_decision=risk_result,
            ),
            request,
        )

    # "At the same time," generate the FidelityTradeTicket by running
    # the same Risk Engine stage a second time against the MANUAL
    # broker capability — never hand-built, always the Risk Engine's own
    # computation. Non-fatal if it fails: the paper-execution path can
    # still proceed even without a Fidelity companion ticket.
    fidelity_ticket = None
    if request.manual_broker_capabilities is not None:
        try:
            manual_result = stages.risk_engine_stage(
                request.proposal, request.portfolio, qa, request.market_data, request.manual_broker_capabilities, limits=request.limits, now=request.now
            )
            fidelity_ticket = manual_result.fidelity_ticket
        except Exception:  # noqa: BLE001 - the Fidelity ticket is a companion artifact, not the paper path itself
            fidelity_ticket = None

    # 5. Order Validator. client_order_id is pinned to the proposal's
    # own stable id, not ApprovedOrder.risk_approval_id (a fresh random
    # id generated on every Risk Engine call) — otherwise re-running the
    # same proposal through the pipeline a second time would generate a
    # brand new client_order_id each time and the idempotency check
    # downstream could never recognize it as a duplicate.
    try:
        place_request = stages.order_validator_stage(
            risk_result.approved_order,
            risk_decision=risk_result.decision,
            approved_contracts=risk_result.approved_contracts,
            broker_capabilities=request.automated_broker_capabilities,
            client_order_id=request.proposal.proposal_id,
        )
    except OrderValidationError as exc:
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "order_validator", str(exc),
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
                risk_decision=risk_result, fidelity_ticket=fidelity_ticket,
            ),
            request,
        )

    # 6. PaperBroker -> Simulated Fill
    try:
        order = await stages.paper_broker.place_order(place_request)
    except Exception as exc:  # noqa: BLE001
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "paper_broker", f"PaperBroker failed: {exc}",
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
                risk_decision=risk_result, fidelity_ticket=fidelity_ticket,
            ),
            request,
        )

    if order.status.value == "rejected":
        reason = stages.paper_broker.get_rejection_reason(order.client_order_id) or "PaperBroker rejected the order"
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.REJECTED, "paper_broker", reason,
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
                risk_decision=risk_result, order=order, fidelity_ticket=fidelity_ticket,
            ),
            request,
        )

    if order.filled_quantity == 0:
        return _record(
            stages,
            PipelineOutcome(
                PipelineStatus.NO_FILL, None, "order accepted but not yet filled",
                quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
                risk_decision=risk_result, order=order, fidelity_ticket=fidelity_ticket,
            ),
            request,
        )

    # 7. Portfolio
    updated_portfolio = stages.portfolio_update_stage(request.portfolio, request.proposal, qa, order)

    status = PipelineStatus.FILLED if order.status.value == "filled" else PipelineStatus.PARTIALLY_FILLED
    outcome = PipelineOutcome(
        status, None, f"{status.value}: {order.filled_quantity} contract(s)",
        quantitative_analysis=qa, devils_advocate_review=da_evaluation, portfolio_manager_decision=pm_evaluation,
        risk_decision=risk_result, order=order, fidelity_ticket=fidelity_ticket, updated_portfolio=updated_portfolio,
    )
    # 8. Database
    return _record(stages, outcome, request)


def _record(stages: PipelineStages, outcome: PipelineOutcome, request: PipelineRequest) -> PipelineOutcome:
    if stages.database is not None:
        stages.database.save(
            DatabaseRecord(
                record_id=str(uuid.uuid4()),
                proposal_id=request.proposal.proposal_id,
                status=outcome.status,
                order=outcome.order,
                fidelity_ticket=outcome.fidelity_ticket,
                created_at=request.now,
            )
        )
    return outcome
