"""PAPER_TRADING_V1.5.15: shared daily-cycle helpers, extracted
UNCHANGED from `scripts/run_validation_cycle.py` (where they were
private, module-level functions) so the new Expanded-Universe Sandbox
runner (`scripts/run_sandbox_cycle.py`) can reuse the exact same
existing-position retrieval, candidate-expiry, and lifecycle-only
safety-check logic the official cycle already uses -- never a second,
independently-written copy of any of it.

This is a mechanical extraction, not a rewrite: every function's body,
docstring, and behavior is byte-for-byte what it already was in
`scripts/run_validation_cycle.py` before this step, with exactly one
addition -- `run_lifecycle_only_safety_check` gained a `cycle_id_prefix`
parameter (default `"validation"`, reproducing the official cycle's own
`f"validation-{date}-lifecycle-{hour}"` id exactly) so a second caller
(the sandbox) can supply its own namespaced prefix
(`"sandbox-validation"`) instead, without copying the function.
`scripts/run_validation_cycle.py`'s own call site passes no override,
so the official cycle's cycle-id format is completely unchanged -- see
the full regression suite (`python -m pytest -q`), which exercises
this exact code path end-to-end and passed, byte-for-byte, both before
and after this extraction."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from src.data.market_calendar import EASTERN, is_market_open, is_trading_day
from src.data.option_chain import OptionChain, merge_option_chains
from src.data.provider import DteWindowOptionChainProvider
from src.lifecycle.policies_library import policies_for_strategy
from src.portfolio.orchestrator import OuterCycleInputs, run_outer_cycle
from src.review.candidates import CandidateStatus
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, PortfolioPosition
from src.strategies.base import StrategyKind
from src.validation.records import OpportunityRecord


def line(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def expire_stale_candidates(review_store, cohort_id: str, validation_store, now: datetime) -> int:
    expired_count = 0
    for candidate in review_store.candidates_awaiting_human(cohort_id=cohort_id):
        if not candidate.is_expired(now):
            continue
        expired = replace(
            candidate, status=CandidateStatus.EXPIRED, resolved_at=now,
            resolution_reason="expired unconfirmed before the next daily cycle swept it",
        )
        review_store.save_candidate(expired)
        validation_store.record_opportunity(
            OpportunityRecord(
                opportunity_id=candidate.candidate_id, cohort_id=candidate.cohort_id,
                ticker=candidate.proposal.ticker, created_at=candidate.created_at, cash_no_trade=False,
                alternatives=(), proposal_id=candidate.proposal.proposal_id,
                quantitative_analysis=candidate.quantitative_analysis, risk_decision=candidate.risk_decision,
                pipeline_status=CandidateStatus.EXPIRED.value,
            )
        )
        expired_count += 1
        line("expired stale candidate", candidate.candidate_id)
    return expired_count


async def fetch_existing_position_chain(
    provider, positions: list[PortfolioPosition], *, now: datetime,
) -> OptionChain:
    """PAPER_TRADING_V1.5.10: one ticker's existing PaperBroker position(s)
    may legitimately be held at ANY DTE -- including below
    `QuantFilterConfig.min_dte` once a position has aged past the
    candidate-entry window it was OPENED under -- so an existing
    position's market-data coverage can never be inferred from that
    entry-time window. `positions` is every currently-open
    `PortfolioPosition` for ONE ticker (the caller groups by `.ticker`);
    this returns one merged chain guaranteed (whenever `provider`
    implements `DteWindowOptionChainProvider`) to include a full chain
    for every DISTINCT expiration date this ticker's positions actually
    hold -- each requested EXACTLY (`min_dte=max_dte=` that one
    position's own real DTE), never a nearest-N substitution and never a
    min..max range spanning multiple expirations (which could still
    silently exclude one of them behind `max_expirations`'s own bound).
    This mirrors `src.review.confirmation._fetch_exact_expiration_chain`
    (V1.5.8's fix for the analogous confirmation-retrieval gap) applied
    to existing positions instead of a single review candidate.

    Merged with the provider's own near-term default (`get_option_chain`)
    first, so a provider that does NOT implement
    `DteWindowOptionChainProvider` (e.g. Alpaca) gets byte-identical
    behavior to pre-V1.5.10 -- this function only ever ADDS coverage, it
    never removes any contract the plain call would already have
    returned. `merge_option_chains` dedupes by `(expiration, strike,
    right)`, so requesting an expiration the plain call already covered
    is a harmless no-op, never a duplicate or a conflicting value.

    This function performs retrieval ONLY -- it never decides whether a
    leg is usable (that stays `src.data.quality_gate.validate_option_chain`
    and `src.portfolio.revaluation.revalue_position`'s unmodified,
    exact-identity `(expiration, strike, right)` contract-index lookup,
    which already fails a position closed to `DATA_INSUFFICIENT` on any
    unmatched/stale leg rather than fabricating or substituting data)."""
    ticker = positions[0].ticker
    chain = await provider.get_option_chain(ticker)
    if isinstance(provider, DteWindowOptionChainProvider):
        for expiration in sorted({p.expiration for p in positions}):
            dte = (expiration - now.date()).days
            exact_chain = await provider.get_option_chain_for_dte_window(
                ticker, min_dte=dte, max_dte=dte, as_of=now.date(),
            )
            chain = merge_option_chains(chain, exact_chain)
    return chain


async def run_lifecycle_only_safety_check(
    *, now: datetime, block_reason: str | None, limits: RiskLimitsConfig, portfolio: Portfolio,
    lifecycle_store, control_loop_store, provider, cycle_id_prefix: str = "validation",
) -> bool:
    """PAPER_TRADING_V1.5.9: existing-position Lifecycle Engine/Risk
    kill-switch monitoring must never be suppressed merely because the
    new-position scan window is closed -- see this module's own
    docstring. Called ONLY by a daily-cycle runner, and only when
    `evaluate_validation_cycle_eligibility` blocked the new-position scan
    AND `portfolio.positions` is non-empty (the zero-position case is a
    safe no-op the caller handles itself, without ever reaching here).

    **Structurally cannot scan, generate a candidate, or fill an order.**
    Calls the exact same, unmodified `src.portfolio.orchestrator
    .run_outer_cycle` the normal scan-eligible cycle below calls, with
    `OuterCycleInputs.opportunity_scan=None` and `skip_opportunity_scan=
    True` -- `_run_opportunity_scan_stage` (orchestrator.py) returns a
    pure no-op for both reasons independently, so there is no way for
    this call to reach `scan_and_rank_opportunities`,
    `evaluate_trade_proposal` for a NEW candidate, or the simulated
    broker's own order-entry method. Nothing here constructs a
    `PaperBroker` at all -- lifecycle evaluation only ever reads
    `Portfolio.positions` (an independent, already-durable domain
    object), never the broker's own fill-simulation state, and nothing
    in this function can mutate either.

    **`provider` is supplied by the caller, never looked up here.**
    The daily-cycle runner constructs it from its own, module-level
    market-data-provider factory reference and passes it in, so a test
    that replaces that reference on the runner's own module reliably
    takes effect here too -- a provider resolved via an import private
    to this module would not see that replacement.

    **Own cycle id, deliberately -- and, since PAPER_TRADING_V1.5.11,
    bucketed by the hour, not the whole day.** Uses
    `f"{cycle_id_prefix}-{market-local date}-lifecycle-{market-local
    hour:02d}"`, never the scan-eligible cycle's own `f"{cycle_id_prefix}-
    {date}"` id -- `run_control_cycle` unconditionally persists a
    `ControlCycleRecord` keyed by whatever cycle id it's given the
    moment it runs, consuming that id's idempotency slot. Sharing the
    scan-eligible id here would let an early, gate-closed safety check
    silently consume the day's REAL opportunity-scan slot, permanently
    blocking that day's actual new-position scan once the window opened
    -- exactly the kind of regression this fix must not introduce. The
    two id FAMILIES are independently idempotent: a second call within
    the SAME market-local hour sees its own hourly record already
    exists and no-ops (a duplicate/retry guard -- documented, not a
    scheduler); a call in a NEW hour -- whether later the same day, or
    after the main cycle has already run today -- gets its own, still-
    untouched hourly id and runs a fresh evaluation; the scan-eligible
    cycle, whenever its own window is open, sees its own, still-
    untouched daily id and proceeds completely normally.
    `cycle_id_prefix` (PAPER_TRADING_V1.5.15) exists ONLY so a second,
    namespaced caller -- the Expanded-Universe Sandbox runner -- can
    supply its own prefix (e.g. `"sandbox-validation"`) and therefore
    its own, structurally non-colliding idempotency-key family, never
    changing the official caller's own id format (its call site passes
    no override, so `cycle_id_prefix` stays `"validation"` there,
    exactly as before this parameter existed). PAPER_TRADING_V1.5.11
    explicitly does NOT add an automatic scheduler, background loop, or
    periodic trigger of any kind -- every lifecycle-only invocation
    remains exactly as manually-initiated (CLI or the one dashboard
    POST route) as it already was; the hourly bucket only changes what
    a manual recheck is ALLOWED to do, never what causes one to happen.
    The hour is computed from `now`'s own America/New_York-local wall-
    clock hour (the same `EASTERN` zoneinfo `src.data.market_calendar`
    already uses throughout this codebase), not from `now`'s own
    timezone (`now` is UTC in every real invocation) -- using UTC's
    hour directly would silently misalign the bucket boundary from the
    market session this whole module is about, and `zoneinfo`-based
    conversion already handles DST correctly with no fixed-offset
    special-casing needed.

    **Fetches ONLY existing positions' tickers**, via
    `fetch_existing_position_chain` (PAPER_TRADING_V1.5.10) -- never the
    scan universe, never a candidate-entry-DTE-window fetch (there is no
    candidate generation to serve). V1.5.10 fix: this now requests EACH
    position's own actual held expiration explicitly (exact DTE, never
    the global [20, 45] candidate window and never a nearest-N
    substitution), so a position that has legitimately aged below that
    window -- or was simply never within reach of the provider's own
    nearest-N default to begin with -- is still retrievable. All
    existing freshness/quality-gate/kill-switch protections apply
    completely unchanged, since this flows through the identical,
    unmodified `run_control_cycle`; a leg this fetch still can't cover
    (provider failure, or genuinely no matching contract) fails closed
    to `DATA_INSUFFICIENT` exactly as before -- this function only ever
    widens what can be successfully retrieved, never what counts as a
    valid match."""
    local_now = now.astimezone(EASTERN)
    lifecycle_cycle_id = f"{cycle_id_prefix}-{local_now.date().isoformat()}-lifecycle-{local_now.hour:02d}"
    if control_loop_store.get_cycle_record(lifecycle_cycle_id) is not None:
        print(f"Lifecycle-only safety check {lifecycle_cycle_id!r} already ran this hour -- nothing to do.")
        return True

    positions_by_ticker: dict[str, list[PortfolioPosition]] = {}
    for p in portfolio.positions:
        positions_by_ticker.setdefault(p.ticker, []).append(p)
    fetch_results: dict[str, OptionChain | Exception] = {}
    try:
        for ticker in sorted(positions_by_ticker):
            try:
                fetch_results[ticker] = await fetch_existing_position_chain(
                    provider, positions_by_ticker[ticker], now=now,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the cycle
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    failed = [t for t, c in fetch_results.items() if isinstance(c, Exception)]
    if failed:
        line("lifecycle-only safety check -- symbols failed (isolated, cycle continues)", failed)
    provider_name = next((c.source for c in fetch_results.values() if isinstance(c, OptionChain)), "unknown")
    provider_health_status = "healthy" if not failed else "degraded"

    policy_name_for_position = {
        p.position_id: policies_for_strategy(StrategyKind(p.strategy.value))[0].name for p in portfolio.positions
    }

    inputs = OuterCycleInputs(
        cycle_id=lifecycle_cycle_id, as_of=now, portfolio=portfolio, limits=limits,
        provider=provider_name, provider_health_status=provider_health_status,
        is_trading_day=is_trading_day(now.date()), is_market_open=is_market_open(now),
        fetch_results=fetch_results, lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
        policy_name_for_position=policy_name_for_position,
        opportunity_scan=None, skip_opportunity_scan=True,
    )
    result = run_outer_cycle(inputs)

    line("lifecycle-only safety check -- existing positions evaluated", result.control_result.cycle_record.positions_evaluated)
    line("lifecycle-only safety check -- lifecycle triggers", result.control_result.cycle_record.lifecycle_triggers)
    line("lifecycle-only safety check -- degraded_mode", result.control_result.cycle_record.degraded_mode)
    if result.new_alerts:
        line("lifecycle-only safety check -- new alerts", len(result.new_alerts))

    print(
        "\nPASS: lifecycle-only safety check complete -- existing positions were evaluated through the "
        "unmodified Lifecycle Engine/Risk kill-switch. New-position scanning was skipped this invocation "
        f"because the new-position scan window is closed ({block_reason})."
    )
    return True
