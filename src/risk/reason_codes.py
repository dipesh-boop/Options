"""The Risk Engine's decision vocabulary: what it decided, and exactly
why. `RiskDecision` is one of four values; `ReasonCode` is the
machine-readable explanation attached to that decision. An LLM may read
these to explain a rejection in prose — it may never produce one itself
and have it mistaken for an authoritative decision (see
`src.risk.engine`, which is the only place a `RiskDecision` is ever
assigned).

Every REJECT/HALT code here corresponds to a specific, deterministic
check somewhere in `src.risk`. `REJECT_UNKNOWN_RISK` is the deliberate
catch-all: "unknown risk is not acceptable risk" means the Risk Engine
must never approve a trade because a check merely didn't run — every
code path either reaches a specific reason or reaches this one.
"""
from __future__ import annotations

from enum import Enum


class RiskDecision(str, Enum):
    APPROVE = "approve"
    RESIZE = "resize"
    REJECT = "reject"
    HALT = "halt"


class ReasonCode(str, Enum):
    # Positive outcomes.
    APPROVED = "approved"
    RESIZED_POSITION_RISK = "resized_position_risk"

    # Input integrity / boundary guards.
    REJECT_MALFORMED_PROPOSAL = "reject_malformed_proposal"
    REJECT_INVALID_CONTRACT = "reject_invalid_contract"
    REJECT_UNSUPPORTED_STRATEGY = "reject_unsupported_strategy"
    REJECT_UNSUPPORTED_ACTION = "reject_unsupported_action"

    # Market data.
    REJECT_STALE_DATA = "reject_stale_data"
    REJECT_LIQUIDITY = "reject_liquidity"

    # Economics.
    REJECT_UNDEFINED_MAX_LOSS = "reject_undefined_max_loss"
    REJECT_QUANT_MISMATCH = "reject_quant_mismatch"

    # Capital / collateral.
    REJECT_INSUFFICIENT_CASH = "reject_insufficient_cash"
    REJECT_BUYING_POWER = "reject_buying_power"
    REJECT_MISSING_COLLATERAL = "reject_missing_collateral"

    # Portfolio-level.
    REJECT_MAX_TRADE_RISK = "reject_max_trade_risk"
    REJECT_UNDERLYING_CONCENTRATION = "reject_underlying_concentration"
    REJECT_SECTOR_CONCENTRATION = "reject_sector_concentration"
    REJECT_CORRELATION = "reject_correlation"
    REJECT_DUPLICATE_POSITION = "reject_duplicate_position"
    REJECT_STRESS_TEST_FAILURE = "reject_stress_test_failure"

    # Broker capability.
    REJECT_ACCOUNT_CAPABILITY = "reject_account_capability"
    REJECT_BROKER_ACCOUNT_MISMATCH = "reject_broker_account_mismatch"

    # Drawdown / kill switch.
    HALT_PORTFOLIO_DRAWDOWN = "halt_portfolio_drawdown"
    HALT_MANUAL_KILL_SWITCH = "halt_manual_kill_switch"
    REJECT_DRAWDOWN_RISK_REDUCTION = "reject_drawdown_risk_reduction"

    # Fail-closed catch-all: any risk that could not be positively
    # calculated resolves here, never to APPROVED.
    REJECT_UNKNOWN_RISK = "reject_unknown_risk"


# Reason codes that may legitimately accompany RiskDecision.APPROVE or
# RiskDecision.RESIZE. Every other code is a REJECT/HALT explanation.
POSITIVE_REASON_CODES = frozenset({ReasonCode.APPROVED, ReasonCode.RESIZED_POSITION_RISK})
