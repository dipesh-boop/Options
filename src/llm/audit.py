"""The Portfolio Manager's append-only decision audit log — the
`AgentCallResult` docstring in `src.llm.client` has flagged this as "not
implemented yet" since the LLM orchestration layer was first built; Step
10 is what finally implements it.

Every entry records what Step 10 requires and nothing else: the model
used, a prompt version, references to the inputs that were evaluated
(IDs, not the full input payloads — the payloads themselves are
reconstructible from those IDs by whatever eventually persists
`TradeProposal`/`RiskDecisionResult`/etc., so duplicating them here would
just be another place for two copies to drift), the decision, a
timestamp, and a concise supporting rationale drawn only from the
decision's own structured fields (`thesis_summary`/`bear_case`). There is
no field anywhere in `DecisionAuditRecord` for raw model chain-of-thought
or "thinking" content — the record can only ever be built from
`PortfolioDecision`'s already-validated, already-boundary-guarded fields,
so there is no code path by which hidden reasoning could end up stored.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from src.llm.portfolio_manager import PortfolioManagerEvaluation
from src.llm.schemas import ensure_portfolio_decision


@dataclass(frozen=True)
class DecisionAuditRecord:
    record_id: str
    model: str
    prompt_version: str
    input_references: dict[str, str]
    decision_id: str
    proposal_id: str
    decision: str  # PortfolioDecisionType, stored as plain str for a stable audit format
    timestamp: datetime
    supporting_rationale: str


class AuditLog(ABC):
    """Append-only by contract: no method on this interface can modify
    or remove a record once written. A concrete implementation may add
    its own internal bookkeeping, but it must never expose an update or
    delete path — see `InMemoryAuditLog`, which quite literally cannot
    (there is no such method to call)."""

    @abstractmethod
    def append(self, record: DecisionAuditRecord) -> None: ...

    @abstractmethod
    def all(self) -> list[DecisionAuditRecord]: ...

    @abstractmethod
    def for_proposal(self, proposal_id: str) -> list[DecisionAuditRecord]: ...


class InMemoryAuditLog(AuditLog):
    """Process-local only — lost on restart, same caveat as
    `src.brokers.base.InMemoryIdempotencyStore`. A placeholder for a
    real, persisted audit table (Phase 0, not built yet), not a
    production guarantee."""

    def __init__(self) -> None:
        self._records: list[DecisionAuditRecord] = []

    def append(self, record: DecisionAuditRecord) -> None:
        self._records.append(record)

    def all(self) -> list[DecisionAuditRecord]:
        return list(self._records)

    def for_proposal(self, proposal_id: str) -> list[DecisionAuditRecord]:
        return [r for r in self._records if r.proposal_id == proposal_id]


def record_decision(
    log: AuditLog,
    evaluation: PortfolioManagerEvaluation,
    *,
    prompt_version: str,
    input_references: dict[str, str],
) -> DecisionAuditRecord:
    """The only way a `PortfolioDecision` becomes an audit entry. Takes
    the already-validated `PortfolioManagerEvaluation`
    (`src.llm.portfolio_manager.evaluate_proposal`'s return value) rather
    than a raw model response, so there is no path into the audit log
    that bypasses `ensure_portfolio_decision`."""
    decision = ensure_portfolio_decision(evaluation.decision)
    rationale = f"{decision.thesis_summary.strip()} Bear case: {decision.bear_case.strip()}"
    record = DecisionAuditRecord(
        record_id=str(uuid.uuid4()),
        model=evaluation.call_result.model,
        prompt_version=prompt_version,
        input_references=dict(input_references),
        decision_id=decision.decision_id,
        proposal_id=decision.proposal_id,
        decision=decision.decision,
        timestamp=decision.timestamp,
        supporting_rationale=rationale,
    )
    log.append(record)
    return record
