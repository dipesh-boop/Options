"""Hypothesis tracking — the concrete record behind "15-20 delta put
credit spreads outperform 25-30 delta spreads during high-IV regimes"
style statements, and the append-only registry that lets
`src.research.overfitting_guards` count how many hypotheses were tested,
rejected, and survived validation/out-of-sample.

A `Hypothesis` is explicitly *not* a production rule (`ARCHITECTURE.md`'s
LLM/Python trust boundary applies here too): nothing in this module can
change `config/risk_limits.yaml`, a strategy's live parameters, or any
file this platform trades against. See `src.research.promotion` for the
only path from a surviving hypothesis to something that could ever
become one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from src.research.performance_breakdown import AnalysisDimension

HypothesisStatus = Literal[
    "proposed",
    "backtested",
    "validated",
    "out_of_sample_tested",
    "rejected",
    "survived_out_of_sample",
]

# The only forward-moving transitions this registry accepts — a status
# can advance or drop straight to "rejected" at any point, but it can
# never move backward (e.g. "survived_out_of_sample" back to "proposed").
_ALLOWED_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    "proposed": frozenset({"backtested", "rejected"}),
    "backtested": frozenset({"validated", "rejected"}),
    "validated": frozenset({"out_of_sample_tested", "rejected"}),
    "out_of_sample_tested": frozenset({"survived_out_of_sample", "rejected"}),
    "survived_out_of_sample": frozenset(),
    "rejected": frozenset(),
}


class InvalidStatusTransitionError(ValueError):
    pass


class UnknownHypothesisError(ValueError):
    pass


class DuplicateHypothesisError(ValueError):
    pass


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    statement: str
    dimensions: tuple[AnalysisDimension, ...]
    parameter_family_key: str
    created_at: datetime


@dataclass
class HypothesisRecord:
    hypothesis: Hypothesis
    status: HypothesisStatus = "proposed"
    trade_count: int | None = None
    notes: list[str] = field(default_factory=list)


class HypothesisRegistry:
    """Process-local (same "lost on restart" caveat as every other
    `InMemory*` store in this codebase) tracker of every hypothesis
    tested in one research session."""

    def __init__(self) -> None:
        self._records: dict[str, HypothesisRecord] = {}

    def register(self, hypothesis: Hypothesis) -> HypothesisRecord:
        if hypothesis.hypothesis_id in self._records:
            raise DuplicateHypothesisError(f"hypothesis_id {hypothesis.hypothesis_id!r} already registered")
        record = HypothesisRecord(hypothesis=hypothesis)
        self._records[hypothesis.hypothesis_id] = record
        return record

    def get(self, hypothesis_id: str) -> HypothesisRecord:
        record = self._records.get(hypothesis_id)
        if record is None:
            raise UnknownHypothesisError(f"no hypothesis registered with id {hypothesis_id!r}")
        return record

    def update_status(
        self, hypothesis_id: str, new_status: HypothesisStatus, *, trade_count: int | None = None, note: str | None = None
    ) -> HypothesisRecord:
        record = self.get(hypothesis_id)
        if new_status not in _ALLOWED_TRANSITIONS.get(record.status, frozenset()):
            raise InvalidStatusTransitionError(
                f"cannot move hypothesis {hypothesis_id!r} from {record.status!r} to {new_status!r}"
            )
        record.status = new_status
        if trade_count is not None:
            record.trade_count = trade_count
        if note:
            record.notes.append(note)
        return record

    def all_records(self) -> tuple[HypothesisRecord, ...]:
        return tuple(self._records.values())

    def count_by_status(self, status: HypothesisStatus) -> int:
        return sum(1 for r in self._records.values() if r.status == status)

    def tested_count(self) -> int:
        """"Tested" means at least one backtest has actually been run —
        a still-`proposed` hypothesis (an idea not yet backtested) does
        not count."""
        return sum(1 for r in self._records.values() if r.status != "proposed")

    def family_count(self, parameter_family_key: str) -> int:
        """How many hypotheses share this family key — the mechanism
        `src.research.overfitting_guards.check_parameter_mining` uses to
        catch "repeatedly testing minor parameter changes until
        something profitable appears.\""""
        return sum(1 for r in self._records.values() if r.hypothesis.parameter_family_key == parameter_family_key)
