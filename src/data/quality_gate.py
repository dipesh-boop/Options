"""Deterministic market-data quality gate (Step 22.4 Part 7).

Sits between a concrete provider (`src.data.tradier_provider`,
`src.data.alpaca_provider`) and anything in the control loop / quant /
risk layers that acts on market data. A provider adapter's own parsing
(e.g. `tradier_provider._parse_option_json`) already refuses to
construct a canonical `OptionContract`/`UnderlyingQuote` at all for the
grossly malformed case (missing required field, unparseable strike,
non-`call`/`put` type) — Pydantic's own field constraints on those
models (`strike > 0`, `bid/ask >= 0`, the `_bid_not_above_ask`
validator, timezone-aware timestamps) already enforce several of Part
7's checks structurally, at construction time, for every provider, not
just Tradier. This module is for everything that survives construction
as a syntactically valid canonical object but is still semantically
unfit for trading logic: NaN/infinite floats (which Pydantic's default
float validation does NOT reject), a crossed/zero/excessively-wide
market that slipped through as `bid=0, ask=0` (not `bid > ask`, so the
model-level validator never fires), staleness, and a fetch whose
contracts don't actually match the underlying/expiration that was
asked for (a provider response-mapping bug, not a market condition).

**Isolation, not all-or-nothing** (Part 7's "must not crash the entire
control loop... isolate failures by symbol/position where safe"):
`validate_option_chain` inspects every contract independently and
partitions them into `valid_contracts` / `rejected`, so one bad quote
in a 342-contract chain costs that one contract, never the chain.
`is_systemic_chain_failure` is the separate, explicit escalation point
for the case where isolation isn't enough (the underlying quote itself
is unusable, or nothing in the chain survived) — the caller treats that
as DATA_INSUFFICIENT for this underlying, never as an invented "looks
fine" result.

Deliberately out of scope here: "missing contracts" (Part 7's list) has
no universal definition without a caller-supplied expectation (which
strikes/expirations a strategy actually needs) — that comparison
belongs to the caller (e.g. `src.workflows.candidate_generation`'s own
liquidity/structure checks), not this general-purpose gate.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict

from src.data.option_chain import OptionChain, OptionContract
from src.data.provider import DEFAULT_MAX_QUOTE_AGE, FreshnessStatus
from src.data.quotes import UnderlyingQuote

# Spread-as-fraction-of-mid thresholds. Deterministic policy, not a
# provider-reported figure: below WARNING the market is treated as
# normal; [WARNING, CRITICAL) is usable but flagged (Part 28 leaves the
# final liquidity call to the caller, e.g. the opportunity scanner);
# >= CRITICAL is not a usable two-sided market at all.
_MAX_SPREAD_PCT_WARNING = 0.50
_MAX_SPREAD_PCT_CRITICAL = 2.00


class QualityIssueSeverity(str, Enum):
    CRITICAL = "critical"  # contract/quote must not be treated as tradable
    WARNING = "warning"  # usable but should be surfaced to the caller/dashboard


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: QualityIssueSeverity
    message: str


def _finite(value: float | None) -> bool:
    return value is None or math.isfinite(value)


def validate_underlying_quote(
    quote: UnderlyingQuote, *, as_of: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE
) -> list[QualityIssue]:
    """Every deterministic check Part 7 asks for that applies to a bare
    underlying quote. Pure — never raises, never mutates `quote`."""
    issues: list[QualityIssue] = []

    for field_name in ("bid", "ask", "last"):
        v = getattr(quote, field_name)
        if not _finite(v):
            issues.append(
                QualityIssue(
                    code=f"non_finite_{field_name}",
                    severity=QualityIssueSeverity.CRITICAL,
                    message=f"{field_name}={v!r} is NaN or infinite",
                )
            )

    if quote.bid > 0 and quote.ask > 0 and quote.bid > quote.ask:
        issues.append(
            QualityIssue(
                code="crossed_market",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"bid ({quote.bid}) exceeds ask ({quote.ask})",
            )
        )

    if quote.bid <= 0 and quote.ask <= 0 and quote.last <= 0:
        issues.append(
            QualityIssue(
                code="zero_market",
                severity=QualityIssueSeverity.CRITICAL,
                message="bid, ask, and last are all zero/unset -- no usable price",
            )
        )

    if quote.freshness_status(as_of, max_age) == FreshnessStatus.STALE:
        issues.append(
            QualityIssue(
                code="stale_quote",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"quote is {quote.age(as_of)} old, exceeding max allowed age {max_age}",
            )
        )

    return issues


def validate_option_contract(
    contract: OptionContract,
    *,
    as_of: datetime,
    expected_underlying: str | None = None,
    expected_expiration: date | None = None,
    max_age: timedelta = DEFAULT_MAX_QUOTE_AGE,
) -> list[QualityIssue]:
    """Every deterministic check Part 7 asks for that applies to one
    option contract, beyond what `OptionContract`'s own Pydantic
    validators already enforce at construction (strike > 0, bid/ask >=
    0, ask >= bid when both are positive, a real `OptionRight`,
    timezone-aware timestamps). Pure — never raises, never mutates
    `contract`.

    `expected_underlying`/`expected_expiration` are how a caller that
    fetched a single-underlying, single-expiration chain (e.g.
    `TradierMarketDataProvider.get_option_chain_for_expiration`) catches
    a provider/mapping bug that put a contract for the wrong
    underlying/expiration into the response -- Part 7's "symbol
    consistency"/"expiration consistency" checks. Pass `None` (the
    default) to skip either check, e.g. for a multi-expiration chain
    where only underlying consistency is meaningful.
    """
    issues: list[QualityIssue] = []

    if expected_underlying is not None and contract.underlying.upper() != expected_underlying.upper():
        issues.append(
            QualityIssue(
                code="underlying_mismatch",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"contract underlying {contract.underlying!r} does not match expected {expected_underlying!r}",
            )
        )

    if expected_expiration is not None and contract.expiration != expected_expiration:
        issues.append(
            QualityIssue(
                code="expiration_mismatch",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"contract expiration {contract.expiration} does not match expected {expected_expiration}",
            )
        )

    for field_name in ("bid", "ask", "last", "strike", "underlying_price", "iv", "delta", "gamma", "theta", "vega"):
        v = getattr(contract, field_name)
        if not _finite(v):
            issues.append(
                QualityIssue(
                    code=f"non_finite_{field_name}",
                    severity=QualityIssueSeverity.CRITICAL,
                    message=f"{field_name}={v!r} is NaN or infinite",
                )
            )

    if contract.volume < 0 or contract.open_interest < 0:  # pragma: no cover - already unreachable, ge=0 on the model
        issues.append(
            QualityIssue(
                code="negative_volume_or_oi",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"volume={contract.volume} open_interest={contract.open_interest}",
            )
        )

    if contract.bid > 0 and contract.ask > 0 and contract.bid > contract.ask:  # pragma: no cover - model already rejects this at construction
        issues.append(
            QualityIssue(
                code="crossed_market",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"bid ({contract.bid}) exceeds ask ({contract.ask})",
            )
        )

    if contract.bid <= 0 and contract.ask <= 0:
        issues.append(
            QualityIssue(
                code="zero_market",
                severity=QualityIssueSeverity.WARNING,
                message="both bid and ask are zero -- no two-sided market; last/volume/OI may still support a "
                "liquidity decision per the caller's own Part 28 policy",
            )
        )
    else:
        mid = contract.mid
        if mid > 0:
            spread_pct = (contract.ask - contract.bid) / mid
            if spread_pct >= _MAX_SPREAD_PCT_CRITICAL:
                issues.append(
                    QualityIssue(
                        code="excessively_wide_market",
                        severity=QualityIssueSeverity.CRITICAL,
                        message=f"spread is {spread_pct:.0%} of mid, exceeding the critical threshold "
                        f"{_MAX_SPREAD_PCT_CRITICAL:.0%}",
                    )
                )
            elif spread_pct >= _MAX_SPREAD_PCT_WARNING:
                issues.append(
                    QualityIssue(
                        code="wide_market",
                        severity=QualityIssueSeverity.WARNING,
                        message=f"spread is {spread_pct:.0%} of mid, exceeding the warning threshold "
                        f"{_MAX_SPREAD_PCT_WARNING:.0%}",
                    )
                )

    if contract.freshness_status(as_of, max_age) == FreshnessStatus.STALE:
        issues.append(
            QualityIssue(
                code="stale_quote",
                severity=QualityIssueSeverity.CRITICAL,
                message=f"contract quote is {contract.age(as_of)} old, exceeding max allowed age {max_age}",
            )
        )

    return issues


class RejectedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    option_symbol: str
    issues: list[QualityIssue]


class ChainQualityResult(BaseModel):
    """The gate's full output for one chain fetch. `valid_contracts` is
    the only thing a caller should hand onward to quant/risk/screening
    logic; `rejected`/`warnings`/`underlying_issues` are for
    logging/alerting/dashboard surfacing, per Part 32/38."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid_contracts: list[OptionContract]
    rejected: list[RejectedContract]
    warnings: dict[str, list[QualityIssue]]
    underlying_issues: list[QualityIssue]
    is_usable: bool


def validate_option_chain(
    chain: OptionChain,
    *,
    as_of: datetime,
    expected_underlying: str | None = None,
    expected_expiration: date | None = None,
    max_age: timedelta = DEFAULT_MAX_QUOTE_AGE,
) -> ChainQualityResult:
    """Validates an already-parsed `OptionChain` contract by contract,
    isolating one bad quote from the rest of the chain rather than
    raising or discarding everything over it (Part 7's core isolation
    requirement). `is_usable=False` means the chain as a whole should be
    treated as DATA_INSUFFICIENT for this underlying (see
    `is_systemic_chain_failure`), not that every individual contract was
    bad.
    """
    underlying_issues = validate_underlying_quote(chain.underlying, as_of=as_of, max_age=max_age)
    resolved_underlying = expected_underlying or chain.underlying.symbol

    valid_contracts: list[OptionContract] = []
    rejected: list[RejectedContract] = []
    warnings: dict[str, list[QualityIssue]] = {}

    for contract in chain.contracts:
        issues = validate_option_contract(
            contract,
            as_of=as_of,
            expected_underlying=resolved_underlying,
            expected_expiration=expected_expiration,
            max_age=max_age,
        )
        critical = [i for i in issues if i.severity == QualityIssueSeverity.CRITICAL]
        warning = [i for i in issues if i.severity == QualityIssueSeverity.WARNING]
        if critical:
            rejected.append(RejectedContract(option_symbol=contract.option_symbol, issues=issues))
            continue
        valid_contracts.append(contract)
        if warning:
            warnings[contract.option_symbol] = warning

    underlying_critical = any(i.severity == QualityIssueSeverity.CRITICAL for i in underlying_issues)
    is_usable = not underlying_critical and len(valid_contracts) > 0

    return ChainQualityResult(
        valid_contracts=valid_contracts,
        rejected=rejected,
        warnings=warnings,
        underlying_issues=underlying_issues,
        is_usable=is_usable,
    )


def is_systemic_chain_failure(result: ChainQualityResult, *, min_valid_contracts: int = 1) -> bool:
    """True for Part 7's "critical systemic data failure" case for one
    underlying's chain fetch -- the caller should record
    DATA_INSUFFICIENT for this underlying/expiration rather than
    attempting to act on whatever individually survived. This is a
    per-symbol signal, never a portfolio-wide one: one underlying's
    systemic failure is isolated here (Part 7), and it is the control
    loop's job (Part 37), not this module's, to decide whether enough
    per-symbol failures in one cycle add up to a broader degraded/HALT
    state."""
    return not result.is_usable or len(result.valid_contracts) < min_valid_contracts
