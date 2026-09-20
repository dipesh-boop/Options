"""Stages 3-4 of `/morning-scan`: load confirmed Fidelity positions, then
reconcile them against the internal portfolio view.

Fidelity is `MANUAL_EXECUTION` forever (`ARCHITECTURE.md`) — this
platform never logs in, scrapes, or automates it in any way. The only
way it is allowed to learn what is actually open in the real account is
a human explicitly confirming it, which is exactly what
`ConfirmedFidelityPosition` represents: data a person typed in after
looking at their own Fidelity account, never fetched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.llm.schemas import StrategyType
from src.risk.portfolio_risk import Portfolio


@dataclass(frozen=True)
class ConfirmedFidelityPosition:
    ticker: str
    strategy: StrategyType
    expiration: date
    strikes: frozenset[float]
    contracts: int
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True)
class ReconciliationDiscrepancy:
    kind: str  # "missing_from_internal" | "missing_from_fidelity" | "quantity_mismatch"
    ticker: str
    detail: str


@dataclass(frozen=True)
class ReconciliationResult:
    matched_count: int
    discrepancies: tuple[ReconciliationDiscrepancy, ...]

    @property
    def clean(self) -> bool:
        return not self.discrepancies


def reconcile_portfolio(confirmed: list[ConfirmedFidelityPosition], portfolio: Portfolio) -> ReconciliationResult:
    """Same identity key `src.risk.portfolio_risk.find_duplicate_position`
    already uses (ticker + strategy + expiration + exact strike set) — a
    position counts as "the same" across both views only if all four
    agree; a quantity difference on an otherwise-matching position is
    reported as its own discrepancy kind, not folded into "missing.\""""
    discrepancies: list[ReconciliationDiscrepancy] = []
    matched = 0
    remaining_internal = list(portfolio.positions)

    for c in confirmed:
        match = next(
            (p for p in remaining_internal if p.ticker == c.ticker and p.strategy == c.strategy and p.expiration == c.expiration and p.strikes == c.strikes),
            None,
        )
        if match is None:
            discrepancies.append(
                ReconciliationDiscrepancy(
                    kind="missing_from_internal", ticker=c.ticker,
                    detail=f"Fidelity confirms {c.contracts} contract(s) of {c.strategy.value} {c.ticker} {c.expiration.isoformat()} not tracked internally",
                )
            )
            continue
        remaining_internal.remove(match)
        if match.contracts != c.contracts:
            discrepancies.append(
                ReconciliationDiscrepancy(
                    kind="quantity_mismatch", ticker=c.ticker,
                    detail=f"Fidelity confirms {c.contracts} contract(s), internal tracks {match.contracts} for {c.strategy.value} {c.ticker} {c.expiration.isoformat()}",
                )
            )
        else:
            matched += 1

    for p in remaining_internal:
        discrepancies.append(
            ReconciliationDiscrepancy(
                kind="missing_from_fidelity", ticker=p.ticker,
                detail=f"internal tracks {p.contracts} contract(s) of {p.strategy.value} {p.ticker} {p.expiration.isoformat()} not confirmed in Fidelity",
            )
        )

    return ReconciliationResult(matched_count=matched, discrepancies=tuple(discrepancies))
