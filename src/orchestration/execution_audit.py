"""Execution-quality audit (Step 12): "Store: theoretical midpoint, paper
fill, Fidelity target limit, minimum acceptable credit, market
timestamp, subsequent price. This will allow us to measure realistic
execution quality."

Same append-only pattern as `src.brokers.base.InMemoryIdempotencyStore`
and `src.llm.audit.InMemoryAuditLog` — a real, persisted table is a
Phase 0 item, not built yet. `subsequent_price` is deliberately not a
mutable field filled in later: since the log is append-only, a later
price observation is recorded as its own follow-up record
(`follow_up_of` pointing back at the original), never an edit to
history.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from src.brokers.base import Order
from src.brokers.fidelity import FidelityTradeTicket


@dataclass(frozen=True)
class ExecutionQualityRecord:
    record_id: str
    proposal_id: str
    theoretical_midpoint: float
    paper_fill_price: float | None
    fidelity_target_limit: float | None
    fidelity_minimum_acceptable_credit: float | None
    market_timestamp: datetime
    recorded_at: datetime
    subsequent_price: float | None = None
    subsequent_price_observed_at: datetime | None = None
    follow_up_of: str | None = None


class ExecutionAuditLog(ABC):
    @abstractmethod
    def append(self, record: ExecutionQualityRecord) -> None: ...

    @abstractmethod
    def all(self) -> list[ExecutionQualityRecord]: ...

    @abstractmethod
    def for_proposal(self, proposal_id: str) -> list[ExecutionQualityRecord]: ...


class InMemoryExecutionAuditLog(ExecutionAuditLog):
    def __init__(self) -> None:
        self._records: list[ExecutionQualityRecord] = []

    def append(self, record: ExecutionQualityRecord) -> None:
        self._records.append(record)

    def all(self) -> list[ExecutionQualityRecord]:
        return list(self._records)

    def for_proposal(self, proposal_id: str) -> list[ExecutionQualityRecord]:
        return [r for r in self._records if r.proposal_id == proposal_id]


def record_execution_quality(
    log: ExecutionAuditLog,
    *,
    proposal_id: str,
    theoretical_midpoint: float,
    market_timestamp: datetime,
    recorded_at: datetime,
    paper_order: Order | None = None,
    fidelity_ticket: FidelityTradeTicket | None = None,
) -> ExecutionQualityRecord:
    """The single entry point for a fresh (non-follow-up) execution
    quality record — pulls `paper_fill_price` from the paper order's own
    `avg_fill_price` and the two Fidelity figures from the ticket's own
    fields, never a value recomputed or guessed at here."""
    record = ExecutionQualityRecord(
        record_id=str(uuid.uuid4()),
        proposal_id=proposal_id,
        theoretical_midpoint=theoretical_midpoint,
        paper_fill_price=paper_order.avg_fill_price if paper_order is not None else None,
        fidelity_target_limit=fidelity_ticket.limit_price if fidelity_ticket is not None else None,
        fidelity_minimum_acceptable_credit=fidelity_ticket.minimum_acceptable_price if fidelity_ticket is not None else None,
        market_timestamp=market_timestamp,
        recorded_at=recorded_at,
    )
    log.append(record)
    return record


def record_subsequent_price(
    log: ExecutionAuditLog,
    *,
    original: ExecutionQualityRecord,
    subsequent_price: float,
    observed_at: datetime,
) -> ExecutionQualityRecord:
    """Appends a follow-up record capturing a later price observation
    against `original` — for measuring, after the fact, how the market
    moved relative to the fill this platform actually got."""
    record = ExecutionQualityRecord(
        record_id=str(uuid.uuid4()),
        proposal_id=original.proposal_id,
        theoretical_midpoint=original.theoretical_midpoint,
        paper_fill_price=original.paper_fill_price,
        fidelity_target_limit=original.fidelity_target_limit,
        fidelity_minimum_acceptable_credit=original.fidelity_minimum_acceptable_credit,
        market_timestamp=original.market_timestamp,
        recorded_at=observed_at,
        subsequent_price=subsequent_price,
        subsequent_price_observed_at=observed_at,
        follow_up_of=original.record_id,
    )
    log.append(record)
    return record
