"""PAPER_TRADING_V1.5.5, Step 4: the purely-additive, in-process
collector `src.workflows.candidate_generation.generate_candidates`
optionally records observations into.

Kept in its own module, separate from `src.workflows.candidate_funnel`
(which depends on `src.portfolio.opportunity_scan.OpportunityScanResult`),
specifically so `src.workflows.candidate_generation` and
`src.portfolio.opportunity_scan` can both import `FunnelDiagnostics`
without a circular import: `opportunity_scan` already imports from
`candidate_generation`, and `candidate_funnel` imports from
`opportunity_scan` -- if `candidate_generation` imported
`FunnelDiagnostics` from `candidate_funnel` directly, that would close
the cycle (`candidate_generation -> candidate_funnel -> opportunity_scan
-> candidate_generation`). This module has no import of its own
`src.portfolio`/`src.workflows` sibling, so no such cycle can occur.

See `src.workflows.candidate_funnel`'s own module docstring for the full
decision-neutrality argument this class's design depends on.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FunnelDiagnostics:
    """One ticker's worth of purely-additive observations from a single
    `generate_candidates` call. Every `record_*` method returns `None`
    and is never consulted by `generate_candidates`/its private helpers
    to decide anything -- each call is appended strictly AFTER the real
    filter/eligibility/construction decision it describes already
    happened, using the exact values that decision already computed."""

    ticker: str
    chain_contracts_seen: int = 0
    chain_stale: bool = False
    expirations_seen: int = 0
    expirations_eligible: int = 0
    # (strategy_tag, event_type, reason) where event_type is one of
    # "attempt" | "ineligible" | "construction_success" | "construction_rejected"
    # | "generation_exception" (PAPER_TRADING_V1.5.7 -- see
    # record_generation_exception below).
    strategy_events: list[tuple[str, str, str | None]] = field(default_factory=list)

    def record_chain(self, *, contracts_seen: int, stale: bool) -> None:
        self.chain_contracts_seen = contracts_seen
        self.chain_stale = stale

    def record_expirations(self, *, seen: int, eligible: int) -> None:
        self.expirations_seen = seen
        self.expirations_eligible = eligible

    def record_strategy_attempt(self, strategy: str) -> None:
        self.strategy_events.append((strategy, "attempt", None))

    def record_strategy_ineligible(self, strategy: str, reason: str) -> None:
        self.strategy_events.append((strategy, "ineligible", reason))

    def record_construction(self, strategy: str, *, success: bool, reason: str | None) -> None:
        self.strategy_events.append(
            (strategy, "construction_success" if success else "construction_rejected", reason)
        )

    def record_generation_exception(self, strategy: str, category: str) -> None:
        """PAPER_TRADING_V1.5.7: a strategy's proposal-construction step
        (`src.workflows.candidate_generation._build_proposal`) raised
        AFTER a liquid, in-range contract was already found -- e.g. the
        2026-10-02 production incident, where `TradeProposal`'s own
        `data_timestamp > timestamp` integrity check rejected a proposal
        built from a cycle-start `now` captured before the market-data
        fetch that produced a later `chain.timestamp`. Before this
        method existed, this failure mode left NO diagnostic at all (see
        `record_construction`'s own call site in `candidate_generation.py`,
        which now only fires once construction has actually succeeded) --
        the candidate simply vanished between `construction_successes`
        and `quant_evaluations` with nothing explaining why.

        `category` must be a small, bounded, non-secret classifier (this
        codebase passes `type(exc).__name__`, e.g. `"ValidationError"`)
        -- never the raw exception message/args, which could in
        principle echo back field values, a provider payload fragment,
        or other content this module has no business persisting into a
        funnel every operator can see."""
        self.strategy_events.append((strategy, "generation_exception", category))
