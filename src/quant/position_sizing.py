"""Position sizing: pure calculation of a suggested contract count given
risk parameters.

This module computes a size; it does not enforce portfolio-level limits
or know about current portfolio state (open positions, concentration,
drawdown) — that is Python Risk Engine's job (not implemented yet, see
IMPLEMENTATION_PLAN.md). An LLM-proposed `contracts_requested`
(src.llm.schemas.TradeProposal) is never authoritative;
`cap_requested_contracts` below is the concrete mechanism by which that
request gets bounded by a deterministically computed maximum.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSizeResult:
    contracts: int
    capital_allocated: float
    risk_allocated: float
    capped_by: str  # "risk_budget" | "capital_budget" | "requested"


def fixed_fractional_size(
    account_equity: float,
    risk_per_trade_pct: float,
    max_loss_per_contract: float,
    *,
    capital_per_contract: float | None = None,
    max_capital_pct: float | None = None,
) -> PositionSizeResult:
    """Size a position so its worst-case loss does not exceed
    `risk_per_trade_pct` of account equity, optionally also capped by a
    max fraction of equity tied up as capital (relevant for
    cash-secured/covered strategies, where capital committed typically
    exceeds max loss)."""
    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if not 0 < risk_per_trade_pct <= 1:
        raise ValueError("risk_per_trade_pct must be in (0, 1]")
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be positive")

    risk_budget = account_equity * risk_per_trade_pct
    contracts = max(math.floor(risk_budget / max_loss_per_contract), 0)
    capped_by = "risk_budget"

    if capital_per_contract is not None and max_capital_pct is not None:
        if capital_per_contract <= 0:
            raise ValueError("capital_per_contract must be positive")
        if not 0 < max_capital_pct <= 1:
            raise ValueError("max_capital_pct must be in (0, 1]")
        capital_budget = account_equity * max_capital_pct
        contracts_by_capital = max(math.floor(capital_budget / capital_per_contract), 0)
        if contracts_by_capital < contracts:
            contracts = contracts_by_capital
            capped_by = "capital_budget"

    return PositionSizeResult(
        contracts=contracts,
        capital_allocated=contracts * (capital_per_contract or 0.0),
        risk_allocated=contracts * max_loss_per_contract,
        capped_by=capped_by,
    )


def cap_requested_contracts(
    requested_contracts: int,
    max_allowed_contracts: int,
    max_loss_per_contract: float,
    capital_per_contract: float = 0.0,
) -> PositionSizeResult:
    """Bound an LLM-requested contract count by a deterministically
    computed maximum (e.g. from `fixed_fractional_size`). Never returns
    more than `max_allowed_contracts`, regardless of what was requested —
    this is the mechanism, not just the policy statement, by which
    `TradeProposal.contracts_requested` stays a request rather than an
    authoritative size."""
    if requested_contracts < 0:
        raise ValueError("requested_contracts cannot be negative")
    if max_allowed_contracts < 0:
        raise ValueError("max_allowed_contracts cannot be negative")
    if max_loss_per_contract < 0:
        raise ValueError("max_loss_per_contract cannot be negative")

    contracts = min(requested_contracts, max_allowed_contracts)
    capped_by = "requested" if contracts == requested_contracts else "risk_budget"

    return PositionSizeResult(
        contracts=contracts,
        capital_allocated=contracts * capital_per_contract,
        risk_allocated=contracts * max_loss_per_contract,
        capped_by=capped_by,
    )
