"""Step 22.5 (PAPER_TRADING_V1.4.4): the ONE function in this codebase that
may call `PaperBroker.place_order` for a NEW position during the Review-Only
90-day validation.

**No LLM review of any kind occurs here, and none is faked.** This
deliberately does NOT call `src.orchestration.pipeline.run_order_pipeline`
(which hard-requires a `devils_advocate_stage`/`portfolio_manager_stage`
callable) -- inventing a deterministic pass-through stage and presenting it
as equivalent to real LLM review was explicitly ruled out. Instead this
module calls the same underlying, unmodified functions
`run_order_pipeline`'s own stages 1/4/5/6/7 are built from, directly, in
the same order, skipping only the two LLM stages:
`src.orchestration.pipeline.default_quant_stage` ->
`src.risk.engine.evaluate_trade_proposal` ->
`src.brokers.order_validator.validate_and_build_order_request` ->
`PaperBroker.place_order` -> `src.orchestration.pipeline
.default_portfolio_update_stage`. The Risk Engine remains the sole,
unmodified authority (CLAUDE.md invariant #1): nothing here can reach
`PaperBroker.place_order` without a fresh `APPROVE`/`RESIZE` from
`evaluate_trade_proposal`, re-run against the CURRENT portfolio.

**Confirmation authorizes only the exact candidate that was reviewed.**
`confirm_candidate` takes a bare `candidate_id` -- no field anywhere in this
module's call path can change the stored `TradeProposal`'s strategy,
strikes, expiration, direction, or quantity. Every material change between
what was reviewed and what the market looks like now (price drift, capital
drift, a Risk Engine re-rejection, a stale/missing quote, an elapsed TTL)
blocks the fill and transitions the candidate to a clearly-named non-fill
status -- never silently adjusted, never filled anyway.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from src.brokers.base import Fill
from src.brokers.order_validator import OrderValidationError, validate_and_build_order_request
from src.brokers.paper import PaperBroker
from src.data.provider import MarketDataProvider
from src.orchestration.pipeline import default_portfolio_update_stage, default_quant_stage
from src.portfolio.account_state import PaperAccountStateStore, PortfolioStore
from src.review.candidates import CandidateReviewStore, CandidateStatus, ConfirmationAttemptRecord, ReviewedCandidate
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.limits import RiskLimitsConfig
from src.risk.reason_codes import RiskDecision
from src.validation.records import OpportunityRecord, to_jsonable
from src.validation.session import ReconciliationFailureRecord, ValidationStore


class ConfirmationOutcome(str, Enum):
    CONFIRMED = "confirmed"
    ALREADY_RESOLVED = "already_resolved"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    DATA_INSUFFICIENT = "data_insufficient"
    REPRICE_REQUIRED = "reprice_required"
    REJECTED_BY_RISK = "rejected_by_risk"
    REJECTED_BY_BROKER = "rejected_by_broker"
    NO_FILL = "no_fill"


# Every outcome except CONFIRMED/NOT_FOUND/ALREADY_RESOLVED maps 1:1 to a
# CandidateStatus the candidate is transitioned to.
_OUTCOME_TO_STATUS = {
    ConfirmationOutcome.EXPIRED: CandidateStatus.EXPIRED,
    ConfirmationOutcome.DATA_INSUFFICIENT: CandidateStatus.DATA_INSUFFICIENT,
    ConfirmationOutcome.REPRICE_REQUIRED: CandidateStatus.REPRICE_REQUIRED,
    ConfirmationOutcome.REJECTED_BY_RISK: CandidateStatus.REJECTED,
    ConfirmationOutcome.REJECTED_BY_BROKER: CandidateStatus.REJECTED,
    ConfirmationOutcome.NO_FILL: CandidateStatus.NO_FILL,
}


@dataclass(frozen=True)
class ConfirmCandidateInputs:
    """No field here can alter the candidate's economics -- `candidate_id`
    is the only thing that selects WHICH trade; everything else is
    infrastructure (stores, market-data access, limits, tolerances)."""

    candidate_id: str
    now: datetime
    market_data_provider: MarketDataProvider
    review_store: CandidateReviewStore
    validation_store: ValidationStore
    portfolio_store: PortfolioStore
    account_state_store: PaperAccountStateStore
    paper_broker: PaperBroker
    limits: RiskLimitsConfig
    automated_broker_capabilities: BrokerCapabilities | None
    max_price_drift_pct: float
    max_capital_required_drift_pct: float


def _record_attempt(
    inputs: ConfirmCandidateInputs, *, outcome: ConfirmationOutcome, detail: str,
    original_quoted_economics: dict, refreshed_economics: dict | None = None,
    paperbroker_result: dict | None = None,
) -> None:
    inputs.review_store.record_confirmation_attempt(
        ConfirmationAttemptRecord(
            attempt_id=str(uuid.uuid4()), candidate_id=inputs.candidate_id, attempted_at=inputs.now,
            outcome=outcome.value, detail=detail, original_quoted_economics=original_quoted_economics,
            refreshed_economics=refreshed_economics, paperbroker_result=paperbroker_result,
        )
    )


def _finalize(
    inputs: ConfirmCandidateInputs, candidate: ReviewedCandidate, *, outcome: ConfirmationOutcome, detail: str,
    original_quoted_economics: dict, refreshed_economics: dict | None = None,
    paperbroker_result: dict | None = None, paper_order_id: str | None = None,
    paper_client_order_id: str | None = None,
) -> ConfirmationOutcome:
    """Every terminal branch (everything except NOT_FOUND/ALREADY_RESOLVED,
    which never reached a real candidate to finalize) goes through here:
    the candidate transitions exactly once, the attempt is logged, and --
    the first and only time this candidate's outcome becomes known -- a
    single `OpportunityRecord` is written to the validation store (Step 22's
    own idempotent, append-only `record_opportunity`)."""
    status = CandidateStatus.CONFIRMED if outcome == ConfirmationOutcome.CONFIRMED else _OUTCOME_TO_STATUS[outcome]
    updated = replace(
        candidate, status=status, resolved_at=inputs.now, resolution_reason=detail,
        paper_order_id=paper_order_id, paper_client_order_id=paper_client_order_id,
    )
    inputs.review_store.save_candidate(updated)
    _record_attempt(
        inputs, outcome=outcome, detail=detail, original_quoted_economics=original_quoted_economics,
        refreshed_economics=refreshed_economics, paperbroker_result=paperbroker_result,
    )
    inputs.validation_store.record_opportunity(
        OpportunityRecord(
            opportunity_id=candidate.candidate_id, cohort_id=candidate.cohort_id, ticker=candidate.proposal.ticker,
            created_at=candidate.created_at, cash_no_trade=False, alternatives=(),
            proposal_id=candidate.proposal.proposal_id, quantitative_analysis=candidate.quantitative_analysis,
            risk_decision=candidate.risk_decision, pipeline_status=outcome.value,
        )
    )
    return outcome


async def confirm_candidate(inputs: ConfirmCandidateInputs) -> ConfirmationOutcome:
    """The revalidate-then-fill sequence. See module docstring for the
    exact function call sequence and why the LLM stages are skipped, not
    faked."""
    candidate = inputs.review_store.get_candidate(inputs.candidate_id)
    if candidate is None:
        return ConfirmationOutcome.NOT_FOUND

    original_economics = {
        "estimated_credit_debit": candidate.risk_decision.approved_order.estimated_credit_debit
        if candidate.risk_decision.approved_order is not None else None,
        "capital_required": candidate.quantitative_analysis.capital_required,
        "max_loss": candidate.quantitative_analysis.max_loss,
    }

    if candidate.status != CandidateStatus.AWAITING_HUMAN:
        # A second confirm attempt on an already-resolved candidate is
        # refused here, before ever reaching PaperBroker -- idempotency's
        # first layer (the second layer is PaperBroker's own
        # SqliteIdempotencyStore, keyed on the same client_order_id).
        _record_attempt(
            inputs, outcome=ConfirmationOutcome.ALREADY_RESOLVED,
            detail=f"candidate is already {candidate.status.value}, not awaiting human confirmation",
            original_quoted_economics=original_economics,
        )
        return ConfirmationOutcome.ALREADY_RESOLVED

    if candidate.is_expired(inputs.now):
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.EXPIRED,
            detail=f"candidate created {candidate.created_at.isoformat()} exceeded its "
            f"{candidate.ttl_seconds}s confirmation TTL as of {inputs.now.isoformat()}",
            original_quoted_economics=original_economics,
        )

    # The current Portfolio is keyed by account_id, re-fetched here (never
    # taken from the candidate's own stale creation-time exposure fields)
    # -- the same account_id the caller used to construct inputs.paper_broker.
    portfolio = inputs.portfolio_store.get(inputs.paper_broker.account_id)
    if portfolio is None:
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.DATA_INSUFFICIENT,
            detail="no current Portfolio state found for this account -- cannot revalidate against unknown state",
            original_quoted_economics=original_economics,
        )

    try:
        chain = await inputs.market_data_provider.get_option_chain(candidate.proposal.ticker)
    except Exception as exc:  # noqa: BLE001 - any fetch failure is DATA_INSUFFICIENT, never a crash
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.DATA_INSUFFICIENT,
            detail=f"fresh market data fetch failed: {exc!r}", original_quoted_economics=original_economics,
        )

    try:
        # Recomputes Quant from the fresh chain -- this alone enforces
        # quote/data freshness (resolve_leg_contracts inside
        # default_quant_stage raises on a stale/missing contract against
        # limits.max_market_data_age_minutes, the same freshness gate
        # every other Quant call in this codebase uses).
        fresh_qa = default_quant_stage(candidate.proposal, chain, portfolio, inputs.limits, now=inputs.now)
    except Exception as exc:  # noqa: BLE001 - stale/missing/malformed data is DATA_INSUFFICIENT, never a crash
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.DATA_INSUFFICIENT,
            detail=f"repricing against fresh data failed: {exc!r}", original_quoted_economics=original_economics,
        )

    fresh_risk = evaluate_trade_proposal(
        candidate.proposal, portfolio, fresh_qa, chain, inputs.automated_broker_capabilities,
        limits=inputs.limits, now=inputs.now,
    )
    refreshed_economics = {
        "estimated_credit_debit": fresh_risk.approved_order.estimated_credit_debit
        if fresh_risk.approved_order is not None else None,
        "capital_required": fresh_qa.capital_required,
        "max_loss": fresh_qa.max_loss,
    }

    if fresh_risk.decision not in (RiskDecision.APPROVE, RiskDecision.RESIZE):
        # Re-running the unmodified Risk Engine against the CURRENT
        # portfolio is exactly what re-verifies limits/drawdown/kill-switch
        # a second time -- evaluate_trade_proposal checks the kill switch
        # and every limit internally on every call.
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.REJECTED_BY_RISK, detail=fresh_risk.message,
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
        )

    original_credit_debit = original_economics["estimated_credit_debit"]
    fresh_credit_debit = refreshed_economics["estimated_credit_debit"]
    if (
        original_credit_debit is not None and fresh_credit_debit is not None
        and abs(original_credit_debit) > 0
        and abs(fresh_credit_debit - original_credit_debit) / abs(original_credit_debit) > inputs.max_price_drift_pct
    ):
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.REPRICE_REQUIRED,
            detail=(
                f"net price drifted from {original_credit_debit:.4f} to {fresh_credit_debit:.4f}, "
                f"exceeding the {inputs.max_price_drift_pct:.1%} tolerance"
            ),
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
        )

    original_capital = original_economics["capital_required"]
    fresh_capital = refreshed_economics["capital_required"]
    if (
        original_capital and fresh_capital is not None
        and abs(fresh_capital - original_capital) / abs(original_capital) > inputs.max_capital_required_drift_pct
    ):
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.REPRICE_REQUIRED,
            detail=(
                f"capital_required drifted from {original_capital:.2f} to {fresh_capital:.2f}, "
                f"exceeding the {inputs.max_capital_required_drift_pct:.1%} tolerance"
            ),
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
        )

    try:
        place_request = validate_and_build_order_request(
            fresh_risk.approved_order, risk_decision=fresh_risk.decision,
            approved_contracts=fresh_risk.approved_contracts,
            broker_capabilities=inputs.automated_broker_capabilities,
            client_order_id=candidate.candidate_id,
        )
    except OrderValidationError as exc:
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.REJECTED_BY_RISK, detail=f"order validation failed: {exc}",
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
        )

    inputs.paper_broker.update_market_data(chain)
    order = await inputs.paper_broker.place_order(place_request)

    if order.status.value == "rejected":
        reason = inputs.paper_broker.get_rejection_reason(order.client_order_id) or "PaperBroker rejected the order"
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.REJECTED_BY_BROKER, detail=reason,
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
            paperbroker_result=to_jsonable(order, type(order)),
        )

    if order.filled_quantity == 0:
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.NO_FILL, detail="order accepted but not yet filled",
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
            paperbroker_result=to_jsonable(order, type(order)),
        )

    try:
        updated_portfolio = default_portfolio_update_stage(portfolio, candidate.proposal, fresh_qa, order)
    except Exception as exc:  # noqa: BLE001 - the fill already happened; never lose it silently (SY-003 doctrine)
        inputs.validation_store.record_reconciliation_failure(
            ReconciliationFailureRecord(
                failure_id=str(uuid.uuid4()), cohort_id=candidate.cohort_id, proposal_id=candidate.proposal.proposal_id,
                detected_at=inputs.now,
                detail=(
                    f"PaperBroker order {order.broker_order_id} filled ({order.filled_quantity} contract(s)) "
                    f"but the Portfolio update failed: {exc!r} -- requires manual reconciliation"
                ),
            )
        )
        inputs.account_state_store.save(inputs.paper_broker.export_state())
        return _finalize(
            inputs, candidate, outcome=ConfirmationOutcome.CONFIRMED,
            detail=f"filled but portfolio update failed and was recorded as a reconciliation failure: {exc!r}",
            original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
            paperbroker_result=to_jsonable(order, type(order)),
            paper_order_id=order.broker_order_id, paper_client_order_id=order.client_order_id,
        )

    inputs.portfolio_store.save(inputs.paper_broker.account_id, updated_portfolio)
    inputs.account_state_store.save(inputs.paper_broker.export_state())

    all_fills = await inputs.paper_broker.get_fills()
    fills = [f for f in all_fills if f.client_order_id == order.client_order_id]
    return _finalize(
        inputs, candidate, outcome=ConfirmationOutcome.CONFIRMED,
        detail=f"filled: {order.filled_quantity} contract(s) at avg price {order.avg_fill_price}",
        original_quoted_economics=original_economics, refreshed_economics=refreshed_economics,
        paperbroker_result={
            "order": to_jsonable(order, type(order)),
            "fills": [to_jsonable(f, Fill) for f in fills],
        },
        paper_order_id=order.broker_order_id, paper_client_order_id=order.client_order_id,
    )
