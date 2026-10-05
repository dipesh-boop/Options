# STEP 23.8 FREEZE REPORT — PAPER_TRADING_V1.5.8

## Status

**PAPER_TRADING_V1.5.8 / SOFTWARE FREEZE VERIFIED**

A narrow safety/integrity release. Cohort `paper-trading-v1.4.3-validation-2026-09-22`
remains ACTIVE, untouched, and unreset by this step. No official
validation cycle, `confirm_candidate.py` invocation against an
operational candidate, or production Tradier call occurred during
development or testing. No risk limit, Quant threshold, liquidity
threshold, DTE policy, ranking policy, candidate-generation behavior, NAV,
or universe was changed.

## A. Root-cause trace of old confirmation behavior

**Old call site.** `src.review.confirmation.confirm_candidate` fetched
fresh market data for revalidation via
`inputs.market_data_provider.get_option_chain(candidate.proposal.ticker)`
— `MarketDataProvider.get_option_chain`'s own contract (see
`src/data/provider.py`) is a nearest-N-expirations fetch: it returns full
chains for a provider-determined set of upcoming expirations, bounded by
`max_expirations`, chosen by calendar proximity to "now" — never
targeted at any particular expiration a caller actually needs.

**The gap.** Nothing in that call, or in `confirm_candidate` around it,
asked the provider for the candidate's own persisted
`TradeProposal.expiration`. If the candidate's expiration happened to
fall outside whatever window the provider's nearest-N heuristic chose to
fetch, confirmation could fail to retrieve the right data purely because
of that window — not because the data was genuinely unavailable. This is
the exact nearest-N limitation `PAPER_TRADING_V1.5.6` already fixed at
scan time (`src.workflows.candidate_generation`, via the new
`DteWindowOptionChainProvider` capability) — this safety boundary,
confirmation, had not yet adopted the same fix.

**What was NOT broken (verified by reading the code, not assumed).**
`src.risk.trade_risk.resolve_leg_contracts`/`_find_contract` (both
**UNMODIFIED** by this step) already matched every leg strictly on
`(underlying, expiration, strike, right)` — the MD-003 fix (Step 17)
specifically added the `underlying` term to this tuple to prevent a
cross-ticker false match — and already raised `ContractNotFoundError` on
any leg that failed to resolve from whatever chain it was given.
`require_fresh_contract` (also unmodified) already enforced
`max_market_data_age_minutes` per leg. So there was never a scenario
where confirmation could silently substitute a wrong contract for a
right one — the existing per-leg matching already failed closed. The
real, narrower risk was architectural: confirmation never explicitly
asked for the right data in the first place, leaving its success
contingent on an unrelated heuristic (calendar-nearest-N) rather than on
a deliberate request for the exact thing it needed.

## B. Exact files changed

- `src/review/confirmation.py` — the only production file touched.
- `src/validation/freeze.py` — `FREEZE_NAME`/`MANIFEST_VERSION` bump to
  `PAPER_TRADING_V1.5.8`/`1.5.8`, plus a pre-existing latent bug fixed in
  passing: the `FreezeManifest.freeze_version` field (a field distinct
  from `manifest_version`) was hardcoded as the literal string `"1.5.7"`
  at its construction site rather than referencing the `MANIFEST_VERSION`
  constant like every other version-bearing field does — meaning it would
  have silently stayed `"1.5.7"` forever regardless of future version
  bumps had this not been caught here. Fixed to read
  `freeze_version=MANIFEST_VERSION`, the same pattern `manifest_version`
  already used. This is software-version/freeze documentation
  infrastructure, not a behavior change to anything a trade depends on.
- `tests/unit/validation/test_freeze.py` — version-string assertions
  bumped to `1.5.8`.
- `VALIDATION_MANIFEST.json` — regenerated (`review_module_hash` and
  `freeze_version` legitimately reflect the above two changes).
- `tests/unit/review/test_confirmation_exact_expiration.py` (new) — 17
  regression tests, see section H.

**Not touched**: `src/risk/trade_risk.py`, `src/risk/engine.py`,
`src/quant/*`, `src/data/tradier_provider.py`, `src/data/provider.py`,
`src/llm/schemas.py`, `src/brokers/*`, `scripts/run_validation_cycle.py`,
`scripts/confirm_candidate.py`, `config/*.yaml`, `src/review/candidates.py`,
`data/options_agent.db` (which, as in every prior freeze, does not exist
in this sandbox — confirmed via `git status --porcelain data/`, empty).

## C. Exact provider/interface design used

**No new interface.** The existing, already provider-neutral
`src.data.provider.DteWindowOptionChainProvider` (built in
`PAPER_TRADING_V1.5.6`, implemented today only by
`TradierMarketDataProvider`) is reused as-is. Its one abstract method,
`get_option_chain_for_dte_window(symbol, *, min_dte, max_dte, as_of,
diagnostics=None) -> OptionChain`, already supports requesting a
single-day window — this step is the first caller to actually use it
that way (`min_dte == max_dte`).

**New helper, `_fetch_exact_expiration_chain`** (`src/review/confirmation.py`):

```python
async def _fetch_exact_expiration_chain(
    provider: MarketDataProvider, ticker: str, expiration: date, *, as_of: datetime,
) -> OptionChain:
    if isinstance(provider, DteWindowOptionChainProvider):
        dte = (expiration - as_of.date()).days
        return await provider.get_option_chain_for_dte_window(ticker, min_dte=dte, max_dte=dte, as_of=as_of.date())
    return await provider.get_option_chain(ticker)
```

- **Provider supports the capability** (production: Tradier): the window
  collapses to exactly the candidate's persisted expiration — one
  calendar date, nothing else, by construction of `min_dte == max_dte`.
  Per `get_option_chain_for_dte_window`'s own pre-existing contract, if
  no provider expiration falls in that single-day window, it returns a
  chain with **zero contracts** rather than substituting one outside the
  window — this function inherits that fail-honest behavior rather than
  reimplementing it.
- **Provider lacks the capability** (any other `MarketDataProvider`,
  including every existing test double that doesn't implement it): falls
  back to the pre-V1.5.8 `get_option_chain` call, byte-for-byte
  unchanged. Confirmed by
  `test_provider_without_window_capability_falls_back_to_get_option_chain_unchanged`.

**Why this design, not a new interface.** The task's own Part 2 directed
"prefer exact-expiration or exact-contract retrieval over adding
confirmation-specific Tradier logic... if the existing provider
interfaces can support this cleanly without a new interface, use them."
`DteWindowOptionChainProvider` already does exactly this when its window
is collapsed to one day — building a second, parallel "exact expiration"
interface would have duplicated logic the existing one already expresses
correctly, and would have meant two provider capabilities doing
overlapping jobs. No Tradier-specific branch, import, or string exists
anywhere in `src/review/confirmation.py`.

## D. Exact identity matching rules

**Two layers, one unchanged, one new.**

1. **New: explicit expiration-presence check**, added immediately after
   the fetch, before any Quant/Risk recomputation:
   ```python
   if not any(c.expiration == candidate.proposal.expiration for c in chain.contracts):
       return _finalize(..., outcome=ConfirmationOutcome.DATA_INSUFFICIENT, ...)
   ```
   This exists to give a distinct, auditable failure reason ("the exact
   expiration could not be refreshed at all") separate from a later
   per-leg mismatch, and to fail closed as early as possible in the
   sequence — directly satisfying the task's Part 3 ("the requested
   expiration must either be returned and verified, or confirmation
   fails closed. No fallback to a different expiration").

2. **Unchanged: per-leg exact match**,
   `src.risk.trade_risk.resolve_leg_contracts`/`_find_contract`, run
   (as before) inside `default_quant_stage`. Matches every leg on
   `(underlying, expiration, strike, right)` — the minimum identity
   needed, confirmed by inspecting `OptionLeg`'s own fields
   (`right`, `strike`, `side`, `quantity_ratio`) and
   `TradeProposal`'s (`ticker`, `expiration`). **Price is never part of
   identity** — `_find_contract` matches structurally first; price only
   enters later, in the separate drift-tolerance check (section G), which
   can reject a correctly-identified contract for having moved too far,
   but never substitutes a different one to find a better price.

**Option symbol.** `OptionLeg` — the type `TradeProposal.legs` is made
of — carries **no `option_symbol` field** (confirmed:
`OptionLeg.model_fields == {"right", "strike", "side", "quantity_ratio"}`,
asserted directly by a new structural test,
`test_option_symbol_is_not_part_of_the_persisted_leg_identity_today`).
There is therefore nothing stored on a `ReviewedCandidate` to
cross-verify a refreshed contract's `option_symbol` against. This is
documented here explicitly, as the task's own Part 1/4 asked for, rather
than silently assumed or fabricated: the real, persisted, verifiable
leg identity is exactly the four fields `_find_contract` already
matches on. No schema field was added to accommodate this — doing so
would have touched `TradeProposal`, which Part 11's behavior-equivalence
boundary places out of scope for this release.

**No substitution of any kind.** No nearest-strike, nearest-expiration,
or same-delta fallback exists anywhere in this diff or in the unmodified
code paths it calls into.

## E. Freshness / fail-closed behavior

**Canonical freshness choke point preserved, untouched.**
`max_market_data_age_minutes` (`config/risk_limits.yaml`) and
`TimestampedModel.require_fresh`/`freshness_status`
(`src/data/provider.py`) were not modified. Every leg resolved from the
exact-expiration-fetched chain still passes through
`require_fresh_contract` exactly as before — a stale OR future-dated
(beyond the existing `_MAX_FUTURE_CLOCK_SKEW` tolerance) contract still
raises `StaleContractError`, caught by `confirm_candidate`'s existing
broad `except Exception` around `default_quant_stage` and mapped to
`DATA_INSUFFICIENT`, exactly as before this step.

**Every fail-closed path in this step terminates in `DATA_INSUFFICIENT`**,
reusing the existing `ConfirmationOutcome`/`CandidateStatus` vocabulary —
no new outcome value was added, since every new failure mode here is
semantically "the data needed to safely re-evaluate this candidate could
not be positively established," which is exactly what
`DATA_INSUFFICIENT` already means in this codebase.

## F. Quant/Risk recomputation path

**Entirely unchanged.** `default_quant_stage` (recomputes economics from
the refreshed chain) and `evaluate_trade_proposal` (the Risk Engine,
re-run against the CURRENT portfolio) are called exactly as before this
step, with exactly the same arguments in the same order — the only thing
that changed is which `OptionChain` is handed to them. The Risk Engine
remains the sole, unmodified final veto (CLAUDE.md invariant #1):
nothing in this diff can reach `PaperBroker.place_order` without a fresh
`APPROVE`/`RESIZE`. Confirmed by two new positive-control tests
(`test_risk_rejection_after_exact_refresh_blocks_the_fill`,
`test_four_leg_iron_condor_with_all_legs_exact_and_fresh_confirms`) and
one drift test
(`test_price_drift_beyond_tolerance_after_exact_refresh_still_reprices`),
all exercised through the new fetch path.

## G. Multi-leg atomicity behavior

**Unchanged, and directly exercised for the first time at 2 and 4 legs
by this step's new tests.** `resolve_leg_contracts` loops over
`proposal.legs` and raises on the FIRST leg that fails to resolve — the
function has no partial-result return path, so a 2-leg or 4-leg proposal
missing even one leg's contract in the refreshed chain raises before
`compute_trade_economics` is ever called, which `confirm_candidate`'s
existing `except Exception` catches as `DATA_INSUFFICIENT` before any
Risk Engine call, before `validate_and_build_order_request`, and before
`PaperBroker.place_order`. Proven directly:
`test_two_leg_spread_with_one_leg_missing_fails_the_entire_confirmation`
(PUT_CREDIT_SPREAD, long leg missing) and
`test_four_leg_iron_condor_with_one_leg_missing_fails_the_entire_confirmation`
(SHORT_IRON_CONDOR, one wing missing) both assert `DATA_INSUFFICIENT`
and zero `PaperBroker` fills. The positive control
(`test_four_leg_iron_condor_with_all_legs_exact_and_fresh_confirms`)
proves that when all four legs ARE exact and fresh, the existing
`PaperBroker` combo-order path still fills all four legs under one
`broker_order_id`, exactly as it did before this step.

## H. Tests added and results

17 new tests, `tests/unit/review/test_confirmation_exact_expiration.py`:

- `TestExactExpirationFetchPathV158` (10 tests): the exact-window
  capability is preferred and called with `min_dte == max_dte` equal to
  the candidate's real DTE (and never falls back to `get_option_chain`
  when the capability exists); a provider without the capability falls
  back unchanged; exact expiration unavailable fails closed; expiration
  mismatch in the returned chain fails closed; persisted strike missing
  fails closed; right mismatch fails closed; underlying mismatch fails
  closed (the MD-003 cross-check, exercised at this boundary); stale
  exact contract fails closed; future-dated contract beyond clock-skew
  tolerance fails closed; a fetch exception is `DATA_INSUFFICIENT`, never
  a crash.
- `TestMultiLegAtomicityV158` (3 tests): 2-leg and 4-leg one-leg-missing
  atomicity (section G), plus the 4-leg positive control.
- `TestRiskAndDriftStillApplyAfterExactRefreshV158` (2 tests): Risk
  rejection and price-drift-REPRICE_REQUIRED both still apply after the
  new fetch path.
- `TestOptionSymbolIdentityDocumentationV158` (1 test): documents that
  `option_symbol` is not part of today's persisted leg identity (section
  D).
- `TestConfirmCandidateScriptNonMutatingArgErrorV158` (1 test): the CLI's
  wrong-argument-count path (the `--help`-shaped preflight case) returns
  before constructing any store, broker, or provider.

**Result**: all 17 pass. Confirmed meaningful (not vacuous) by running
them against the pre-fix code: 3 of the 17 fail on the unmodified
`src/review/confirmation.py` (the ones directly proving the exact-window
path is used and that an exact-expiration-unavailable/mismatched-expiration
chain is rejected) — exactly the behavior this step changes — while the
other 14 already passed unmodified (they exercise logic, like
`_find_contract`'s strike/right/underlying matching and the drift/Risk
checks, that was already correct before this step and remains correct
after it).

No test in this file, or anywhere else touched by this step, contacts
Tradier production or any network endpoint; every chain is a synthetic,
in-memory `OptionChain` constructed directly in the test module, and
every store used is `InMemory*`.

## I. Full-suite result

`python -m pytest -q`: **3762 passed, 6 skipped, 9 failed** — the same 9
failures, byte-for-byte, present on the unmodified `PAPER_TRADING_V1.5.7`
baseline before this step's changes (confirmed directly: `git stash` +
re-run reproduces the identical 9-failure set). All 9 are pre-existing,
environment/time-of-day-dependent (`test_market_hours_gate.py`'s mocked
market-hours tests; `test_opportunity_evaluation_timestamp.py`/
`test_review_only_daily_cycle.py`'s date-drift-dependent fixtures tied to
the sandbox's advancing real calendar date; `test_run_validation_cycle_cli.py`,
`test_frontend_control_center.py`, `test_operator_status.py` similarly)
and are unrelated to `src/review/`, `src/data/provider.py`, or
`src/validation/freeze.py` — none of which this step's root cause touches.
**Zero new failures introduced by this step.**

## J. Freeze-verification result

`make verify-freeze` → **PAPER_TRADING_V1.5.8 / SOFTWARE FREEZE VERIFIED**,
every check `[OK]`. Before manifest regeneration, `review_module_hash`
legitimately showed `[FAIL] DRIFTED since freeze` — the correct and
expected consequence of genuinely modifying `src/review/confirmation.py`,
the same pattern every prior freeze bump with real production changes has
shown (most recently V1.5.6/V1.5.7). After `make freeze-manifest`
regenerated `VALIDATION_MANIFEST.json`, every check — including
`review_module_hash` itself and every CLAUDE.md-invariant-shaped check
(`fidelity_manual_execution_only`, `live_trading_disabled`,
`daily_cycle_never_calls_place_order`, `dashboard_cannot_confirm_candidates`,
`market_hours_gate_precedes_mutation`, and all others) — passes.
`validation_cohort_started: False`, confirming this freeze process itself
never starts, resets, or modifies a cohort.

## K. Implementation commit hash

See repository log — the commit immediately preceding the freeze-artifacts
commit for this step.

## L. Freeze commit hash

See repository log — this report, `VALIDATION_MANIFEST.json`,
`progress.md`, and the `src/validation/freeze.py`/
`tests/unit/validation/test_freeze.py` version bumps.

## M. Whether branch was pushed

Reported in the final chat summary, with actual outcome (success or
failure) stated honestly — never assumed in advance.

## N. Tag status

`paper-trading-v1.5.8` created locally after verification. Tag push
historically fails with HTTP 403 in this environment (every prior
freeze, V1.5.6/V1.5.7 included) — reported honestly if it recurs, never
worked around, never force-pushed.

## O. Explicit confirmation

- Active cohort `paper-trading-v1.4.3-validation-2026-09-22`: unchanged —
  no code in this diff calls `start_new_cohort` or any reset/reinit path.
- `data/options_agent.db`: not mutated — it does not exist in this
  sandbox (confirmed via `git status --porcelain data/`), and nothing in
  this step's tests touches a real database file; every test uses
  `InMemory*` stores or synthetic in-process `OptionChain` objects.
- No official validation cycle (`scripts/run_validation_cycle.py`) was
  run.
- No production Tradier call, or any network call of any kind, was made
  anywhere in this step's development or testing.
- No candidate was confirmed against an operational cohort;
  `scripts/confirm_candidate.py` was not invoked against any real
  candidate id — the one new test touching that script
  (`test_wrong_argument_count_never_touches_any_store`) only exercises
  its argument-count short-circuit, which returns before constructing
  any store or provider.
- No `PaperBroker` operational position was opened — every fill in every
  new test occurred against an `InMemoryPortfolioStore`/fresh in-process
  `PaperBroker` instance constructed solely for that test.
- No real brokerage order of any kind was placed or could be — Fidelity
  remains `MANUAL_ONLY`; `TradierMarketDataProvider` remains
  market-data-only (unmodified, `tradier_market_data_only` freeze check
  still `[OK]`).
- No risk, Quant, liquidity, or DTE threshold was changed —
  `config/risk_limits.yaml` and every file under `src/risk/`/`src/quant/`
  are untouched by this diff.
- No strategy, universe, or NAV change — `config/universe.yaml`,
  `config/brokers.yaml`, `config/validation.yaml` are untouched.

## P. Remaining known issue

The market-hours-gate-vs-lifecycle-monitoring separation (first
identified V1.5.1, reaffirmed every step since, including
`STEP_23_7_FREEZE_REPORT.md`) remains intentionally unresolved in
`PAPER_TRADING_V1.5.8` and is planned separately, per this step's own
explicit scope boundary ("DO NOT fix the lifecycle market-hours issue in
this release"). This step closes the OTHER previously-flagged
architecture issue — `src.review.confirmation`'s nearest-N refresh — in
full; the market-hours gate issue is the only one of the two
historically-flagged items still open.
