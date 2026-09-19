"""Pre-execution refresh (Step 12): the check that runs right before a
`FidelityTradeTicket` is treated as final and handed to a human. Time
passes between when a ticket was first generated and when a human
actually looks at it — this module re-derives everything from scratch
against current market data and refuses to finalize a ticket whose
numbers no longer hold.

Explicitly, per the spec: refresh the quote, refresh the option legs,
recalculate the spread, recalculate max loss, recalculate Greeks, rerun
Risk Engine. If price moved materially: `REPRICE_REQUIRED` — never hand
a human a ticket built from stale numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from src.brokers.fidelity import FidelityTradeTicket
from src.data.option_chain import OptionChain
from src.llm.schemas import TradeProposal
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import RiskDecisionResult
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import QuantitativeAnalysis

RefreshStatus = Literal["OK", "REPRICE_REQUIRED", "REJECTED"]

# Materiality thresholds for the pre-execution refresh specifically —
# distinct from src.risk.limits (which gates whether a trade is allowed
# at all) and from src.llm.devils_advocate's staleness thresholds (which
# gate an LLM agent's own verdict). This check's only job is "would a
# human be looking at meaningfully different numbers than what was
# already shown them," so it compares the *ticket's own figures*
# (limit price, max loss), not raw quote movement.
DEFAULT_MAX_CREDIT_MOVE_PCT = 0.10  # target/limit credit moved more than 10%
DEFAULT_MAX_MAX_LOSS_MOVE_PCT = 0.10  # max loss moved more than 10%


@dataclass(frozen=True)
class RefreshResult:
    status: RefreshStatus
    reason: str
    ticket: FidelityTradeTicket | None
    risk_decision: RiskDecisionResult | None
    quantitative_analysis: QuantitativeAnalysis | None


def refresh_and_finalize_fidelity_ticket(
    *,
    proposal: TradeProposal,
    portfolio: Portfolio,
    limits: RiskLimitsConfig,
    original_ticket: FidelityTradeTicket,
    fresh_market_data: OptionChain,
    manual_broker_capabilities: BrokerCapabilities,
    quant_stage: Callable[[TradeProposal, OptionChain, Portfolio, RiskLimitsConfig], QuantitativeAnalysis],
    risk_engine_stage: Callable[..., RiskDecisionResult],
    now: datetime,
    max_credit_move_pct: float = DEFAULT_MAX_CREDIT_MOVE_PCT,
    max_max_loss_move_pct: float = DEFAULT_MAX_MAX_LOSS_MOVE_PCT,
) -> RefreshResult:
    """Refreshes quote/legs/spread/max-loss/Greeks (via `quant_stage`)
    and reruns `risk_engine_stage` against `fresh_market_data`, then
    compares the newly generated ticket's key figures against
    `original_ticket`. A material move returns `REPRICE_REQUIRED` and
    withholds the new ticket as final — a human should see an explicit
    reprice notice, not a silently updated number."""
    try:
        qa = quant_stage(proposal, fresh_market_data, portfolio, limits)
    except Exception as exc:  # noqa: BLE001 - refresh failing is itself grounds to withhold the ticket
        return RefreshResult(status="REJECTED", reason=f"could not refresh quantitative analysis: {exc}", ticket=None, risk_decision=None, quantitative_analysis=None)

    try:
        risk_result = risk_engine_stage(proposal, portfolio, qa, fresh_market_data, manual_broker_capabilities, limits=limits, now=now)
    except Exception as exc:  # noqa: BLE001
        return RefreshResult(status="REJECTED", reason=f"could not rerun Risk Engine: {exc}", ticket=None, risk_decision=None, quantitative_analysis=qa)

    if risk_result.decision in (RiskDecision.REJECT, RiskDecision.HALT):
        return RefreshResult(status="REJECTED", reason=risk_result.message, ticket=None, risk_decision=risk_result, quantitative_analysis=qa)

    fresh_ticket = risk_result.fidelity_ticket
    if fresh_ticket is None:
        return RefreshResult(
            status="REJECTED", reason="Risk Engine did not produce a refreshed Fidelity ticket", ticket=None,
            risk_decision=risk_result, quantitative_analysis=qa,
        )

    credit_move_pct = _relative_move(original_ticket.limit_price, fresh_ticket.limit_price)
    max_loss_move_pct = _relative_move(original_ticket.max_loss, fresh_ticket.max_loss)

    if credit_move_pct > max_credit_move_pct or max_loss_move_pct > max_max_loss_move_pct:
        return RefreshResult(
            status="REPRICE_REQUIRED",
            reason=(
                f"target credit moved {credit_move_pct:.1%} (limit {max_credit_move_pct:.1%}) and/or max loss moved "
                f"{max_loss_move_pct:.1%} (limit {max_max_loss_move_pct:.1%}) since the ticket was first generated"
            ),
            ticket=None,
            risk_decision=risk_result,
            quantitative_analysis=qa,
        )

    return RefreshResult(status="OK", reason="within materiality thresholds", ticket=fresh_ticket, risk_decision=risk_result, quantitative_analysis=qa)


def _relative_move(original: float, updated: float) -> float:
    if original == 0:
        return 0.0 if updated == 0 else float("inf")
    return abs(updated - original) / abs(original)
