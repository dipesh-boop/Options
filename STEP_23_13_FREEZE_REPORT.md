# STEP 23.13 FREEZE REPORT — PAPER_TRADING_V1.5.13

## Status

**PAPER_TRADING_V1.5.13 / SOFTWARE FREEZE VERIFIED**

Adds an explicit, read-only `--diagnostic-scan` mode to `scripts/
run_validation_cycle.py`, so an operator can verify a software fix
(immediately: PAPER_TRADING_V1.5.12's proposal-id fix) against LIVE
Tradier production market data and the real candidate -> Quant -> Risk
pipeline, WITHOUT running -- or counting as -- an official validation
day. Cohort `paper-trading-v1.4.3-validation-2026-09-22` remains
ACTIVE, untouched, and unreset by this step. The 2026-10-06 official
cycle was NOT rerun, replaced, or altered. No official validation
cycle, no `--diagnostic-scan` invocation against live Tradier
production, and no production Tradier network call of any kind
occurred during development -- every test in this release runs
against fakes/fixtures only. No risk limit, Quant threshold, liquidity
threshold, candidate DTE policy, universe, enabled strategies,
candidate ranking, position sizing, market-hours behavior, lifecycle
behavior, Tradier/Fidelity behavior, human-confirmation requirement,
PaperBroker execution behavior, or the V1.5.12 `proposal_id_prefix`
fix was changed. Starting NAV remains $100,000.

## A. Root architectural approach

`run_diagnostic_scan()` is READ-ONLY **by construction, not by
convention**. It calls `src.portfolio.opportunity_scan
.scan_and_rank_opportunities` DIRECTLY -- the exact same pure function
the official cycle's own `_run_opportunity_scan_stage` calls, never a
reimplemented screen -- and it never calls `run_outer_cycle`/
`run_control_cycle` at all. Both of those were confirmed by source
inspection to unconditionally persist a `ControlCycleRecord`, lifecycle
positions, decision snapshots, and alerts even for an existing-
position-only evaluation; there is no configuration of either that
stays read-only, so the diagnostic simply never calls them. The only
Sqlite-backed store the function touches at all is
`SqlitePortfolioStore`, and only through its read-only `.get()` --
`.save()` is never called on it, or on anything else, anywhere in the
function body (verified both by a source-level negative-capability
test and a behavioral monkeypatch-and-raise test; see §Q item G).

## B. Exact production files changed

- `scripts/run_validation_cycle.py` -- the new `run_diagnostic_scan()`
  entry point, `_sanitize_diagnostic_exception_message()`,
  `_print_diagnostic_banner()`/`_DIAGNOSTIC_BANNER`, the new
  `--diagnostic-scan` CLI flag (mutually exclusive with `--preflight`),
  and `main()`'s dispatch wiring.
- `src/portfolio/opportunity_scan.py` -- `scan_and_rank_opportunities`
  gained one new, optional, additive keyword parameter,
  `on_generation_exception: Callable[[str, str, Exception], None] |
  None = None`, forwarded into `generate_candidates` per ticker.
- `src/workflows/candidate_generation.py` -- `generate_candidates`
  gained the matching optional, additive keyword parameter,
  `on_generation_exception: Callable[[str, Exception], None] | None =
  None`, called immediately after the existing (unmodified)
  `diagnostics.record_generation_exception(...)` call in all three
  strategies' exception handlers.
- `Makefile` -- new `diagnostic-scan` target (`./scripts/run_validation
  _cycle.sh --diagnostic-scan`), `.PHONY` updated.
- `src/validation/freeze.py` -- `FREEZE_NAME`/`MANIFEST_VERSION`
  bumped from `PAPER_TRADING_V1.5.12`/`1.5.12` to
  `PAPER_TRADING_V1.5.13`/`1.5.13`, with a new changelog comment block.
- `tests/unit/validation/test_freeze.py` -- version assertions bumped
  to `1.5.13`.
- `VALIDATION_MANIFEST.json` -- regenerated via `make freeze-manifest`.
- `tests/acceptance/test_diagnostic_scan_v1513.py` (new) -- the full
  item A-O acceptance suite (§F).
- `STEP_23_13_FREEZE_REPORT.md` (this file) -- new.
- `progress.md` -- new entry.

No changes to `src/risk/`, `src/quant/`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/llm/schemas.py`
(`TradeProposal.proposal_id`'s `max_length=64` and `_next_id()`
untouched), `src/lifecycle/`, `src/llm/` beyond pre-existing imports,
`src/brokers/fidelity.py`, `config/*.yaml`, `scripts/confirm_candidate.py`,
`src/review/`, or `data/options_agent.db`.

## C. Diagnostic entry point

`async def run_diagnostic_scan(*, now: datetime | None = None) -> bool`
in `scripts/run_validation_cycle.py`. `now` defaults to `None` in
every real invocation (identical to `run_validation_cycle`'s own
existing parameter, which exists solely for deterministic
market-hours-gate testing).

## D. Zero-persistence by construction

No forbidden name (`PaperBroker(`, `SqliteControlLoopStore`,
`SqliteLifecycleStore`, `SqliteCandidateReviewStore`,
`SqliteIdempotencyStore`, `SqliteValidationStore`, `run_outer_cycle`,
`run_control_cycle`, `ReviewedCandidate(`, `confirm_candidate(`,
`.save(`) appears anywhere in the function's executable body --
verified by an `ast`-based source scan (stripping the docstring first,
since the docstring intentionally NAMES these forbidden symbols in
plain English to document their absence) that fails loudly if any one
is ever introduced. This is reinforced behaviorally: a test
monkeypatches `SqlitePortfolioStore.save` to raise `AssertionError` if
called at all, then runs a full diagnostic scan (including a
Quant/Risk-surviving candidate) and asserts success -- proving `.save`
is never reached, not merely absent from a grep.

## E. `run_outer_cycle` usage

**Not used at all.** Confirmed by source inspection that
`run_outer_cycle` (`src.portfolio.orchestrator`) unconditionally calls
`run_control_cycle` (which itself unconditionally writes
`lifecycle_store.save_position`/`control_loop_store
.append_decision_snapshot` per position) and additionally calls
`inputs.control_loop_store.save_exposure_snapshot`,
`append_decision_snapshot` (for the opportunity decision), and
`save_alert` for every new alert. There is no configuration under
which it stays read-only, so `run_diagnostic_scan` never calls it,
satisfying the task's explicit preference not to use it if it
currently writes.

## F. `PaperBroker` instantiation

**No.** `PaperBroker` is never imported or constructed anywhere in
`run_diagnostic_scan`. Verified by the source-level negative-capability
scan in §D.

## G. Sqlite write-capable store instantiation

**No write-capable store is instantiated.** The only store constructed
is `SqlitePortfolioStore`, used exclusively via its read-only `.get()`.
No `SqliteControlLoopStore`, `SqliteLifecycleStore`,
`SqliteCandidateReviewStore`, `SqliteIdempotencyStore`, or
`SqliteValidationStore` is ever constructed in diagnostic mode.

## H. Existing portfolio loaded read-only

`portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)`;
`portfolio = portfolio_store.get(ops.account_id)`. If a portfolio
exists (the real situation for the active cohort), it is used exactly
as returned, never re-saved. If none exists, an in-memory `Portfolio`
is constructed from `config/validation.yaml`'s own
`starting_capital.default_nav` -- the same value the official cycle's
own bootstrap uses -- and is never passed to `.save()` anywhere.

## I. Tradier production preflight

`verify_official_provider_is_tradier_production()` -- the exact same,
unmodified, config-only (no network call, no provider instance)
preflight function `run_validation_cycle`/`run_preflight` already use
-- is called first, before any other diagnostic-specific logic. A
mock/sandbox/missing-token configuration raises
`OfficialProviderPreflightError`, which the diagnostic catches and
reports as `FAIL`, returning `False` without ever constructing a
provider.

## J. Market-hours gating before provider construction

`evaluate_validation_cycle_eligibility(...)` -- the exact same
function the official cycle uses -- is called immediately after the
Tradier preflight and strictly BEFORE `get_configured_market_data_
provider()` is ever called. If `eligibility.validation_cycle_allowed`
is `False`, the function prints the block reason and returns `False`
immediately; the market-data provider is never constructed and zero
Tradier calls occur. Unlike the official cycle, there is deliberately
no fallthrough to a lifecycle-only check on a closed gate -- diagnostic
scope is new-position opportunity-scan diagnostics only.

## K. DTE-aware retrieval preserved

The fetch loop checks `isinstance(provider, DteWindowOptionChainProvider)`
and, when true, calls `provider.get_option_chain_for_dte_window(ticker,
min_dte=quant_filter.min_dte, max_dte=quant_filter.max_dte,
as_of=now.date())` -- the exact same PAPER_TRADING_V1.5.6 fix path --
falling back to plain `get_option_chain` only for a provider that
doesn't implement the DTE-aware interface. Never a hardcoded
provider-name check.

## L. Post-fetch `evaluation_as_of` capture

`evaluation_as_of = datetime.now(timezone.utc)` is captured only after
the entire per-ticker fetch loop (and its `finally: await
provider.close()`) completes -- the exact PAPER_TRADING_V1.5.7 fix
pattern -- guaranteeing `evaluation_as_of >= every chain.timestamp` by
construction, never by chance, so every `TradeProposal`'s own
`data_timestamp <= timestamp` integrity check passes.

## M. `proposal_id_prefix` preservation

`scan_and_rank_opportunities(..., proposal_id_prefix="validation-scan",
...)` -- the exact, bare, V1.5.12-corrected literal, never the
redundantly-dated pre-V1.5.12 form. Confirmed directly by a spy-wrapped
call to the real `scan_and_rank_opportunities` asserting the received
`proposal_id_prefix` keyword value.

## N. Diagnostic exception sanitization

A new, diagnostic-only, purely additive `on_generation_exception`
callback (threaded through `generate_candidates` and
`scan_and_rank_opportunities` as an optional parameter, `None` at
every existing/official call site) lets `run_diagnostic_scan` capture
`(ticker, strategy, exc)` for every generation-exception at the exact
point `FunnelDiagnostics.record_generation_exception` already fires
(by design, recording only the bounded exception class). The
diagnostic's own `_sanitize_diagnostic_exception_message(exc)` then
redacts `Bearer <token>`, `Authorization: ...`, and any
`*token*`/`*secret*`/`*api[-_]key*` key-value pattern (case-
insensitive), collapses newlines, and truncates to 400 characters,
before printing `(ticker, strategy, sanitized message)`. The raw
message is never fed back into `FunnelDiagnostics`/`CandidateFunnel`
(whose persisted schema is unchanged), and never logged or persisted
anywhere -- it exists only in this one diagnostic print statement.

## O. Candidate-funnel output produced

Printed via the real `src.workflows.candidate_funnel.build_candidate_
funnel` (the same pure aggregator the official cycle's
`collect_candidate_funnel=True` path uses, with `candidates_persisted`
hardcoded to `0`): symbols scanned; chains usable/failed; contracts
examined; expirations seen/eligible/rejected; strategy attempts/
ineligible; construction attempts/successes/rejections; generation
exceptions (count, plus each sanitized per-exception detail line);
quant evaluations/pass/reject; risk evaluations/pass/reject;
candidates generated/ranked; rejection reasons; by-symbol summary;
by-strategy summary; and either `best candidate: none -- <reason>` or
a `DIAGNOSTIC CANDIDATE SURVIVED QUANT/RISK (NOT PERSISTED, NOT
CONFIRMABLE)` line with ticker/strategy/proposal_id length/risk
decision and an explicit note that no `ReviewedCandidate`,
confirmation command, or PaperBroker order/fill resulted. The
`DIAGNOSTIC SCAN ONLY -- NOT AN OFFICIAL VALIDATION CYCLE / NO
VALIDATION STATE WILL BE MUTATED / RESULTS DO NOT COUNT TOWARD THE
90-DAY VALIDATION` banner prints at both the very start and the very
end of every invocation, success or failure.

## P. Economically realistic PUT_CREDIT_SPREAD test fixture

The new acceptance suite's `_spy_pcs_chain` fixture (590/587 strikes,
short delta -0.15, long delta -0.08) was deliberately chosen, and
verified directly against the real `generate_candidates ->
default_quant_stage -> evaluate_trade_proposal` pipeline with
`internal_paper` broker capabilities, to clear Quant/Risk with a
genuinely positive risk-adjusted return (capital_required=$180,
expected_value=+$27.39, probability_of_profit=0.691,
decision=APPROVE) -- not merely to construct cleanly. A shallower
600/595 spread at the same spot/IV/DTE (closer to V1.5.12's own
regression fixture) reproduces the proposal-id fix fine but prices to
a negative risk-adjusted return and is correctly never selected as
`best`; that is real Quant/Risk economics working as designed, not a
fixture bug, so it would have been the wrong fixture for the tests
that also need a surviving candidate (items I, M).

## Q. New/updated tests

**`tests/acceptance/test_diagnostic_scan_v1513.py`** (new, 23 tests,
offline, fakes/fixtures only):
- **A** `TestHelpSafety` (2) -- `--help` never mutates, never calls
  `run_diagnostic_scan`.
- **B** `TestPreflightSafety` (1) -- `--preflight` never calls
  `run_diagnostic_scan`.
- **C** `TestDiagnosticScanDispatch` (2) -- `--diagnostic-scan` invokes
  only `run_diagnostic_scan`; mutually exclusive with `--preflight`
  (argparse exit code 2).
- **D** `TestOfficialDefaultPathUnchanged` (1) -- no arguments still
  invokes only `run_validation_cycle`.
- **E** `TestDiagnosticProviderPreflight` (4) -- mock/missing-token/
  sandbox all refused; Tradier production accepted.
- **F** `TestDiagnosticMarketHoursGate` (2) -- closed gate: zero
  provider construction, zero persistence; open gate: fetches and
  closes the provider.
- **G** `TestDiagnosticSourceNeverReferencesForbiddenPersistence` (1,
  static) + `TestDiagnosticNeverPersists` (2, behavioral) -- the
  source scan and the monkeypatch-and-raise/no-candidate-review-record
  tests from §D.
- **H** `TestOperationalDbIntegrity` (1) -- SHA-256 of the operational
  DB file is byte-identical before/after a diagnostic run, with the
  table pre-seeded so `CREATE TABLE IF NOT EXISTS` is a true no-op.
- **I** `TestV1512ProposalIdRegressionViaDiagnostic` (1) -- the
  representative PCS reaches construction with zero generation
  exceptions, no `ValidationError` in output, exactly one scan date in
  the printed id, and `len(proposal_id) <= 64`.
- **J** `TestExactPipelineReuse` (1) -- a spy-wrapped
  `scan_and_rank_opportunities` is called exactly once with
  `proposal_id_prefix="validation-scan"`; `run_outer_cycle` raises if
  ever called.
- **K** `TestDteAwareRetrieval` (1) -- `provider.dte_window_calls ==
  [("SPY", 20, 45)]`; `plain_calls == []`.
- **L** `TestPostFetchEvaluationTimestamp` (1) -- a provider that
  stamps every chain with the REAL instant at fetch time (mirroring
  `test_opportunity_evaluation_timestamp.py`'s own
  `FakeLaggyMarketDataProvider` pattern) still produces a candidate
  with no `ValidationError`.
- **M** `TestCandidateSurvivorIsDiagnosticOnly` (1) -- survivor is
  printed but never persisted, no `confirm_candidate.py` text appears,
  no candidate-review or idempotency record is created.
- **N** `TestRepeatedDiagnosticNeverBlocksTheOfficialCycle` (1) -- two
  diagnostic runs create no official cycle record; the official cycle
  then runs normally afterward.
- **O** `TestOfficialBehaviorEquivalence` (1) -- `scan_and_rank_
  opportunities` called with and without the new
  `on_generation_exception` callback produces byte-identical
  `scanned`/`best`/`no_trade_reason`, and the callback never fires for
  an error-free candidate.

## R. Focused test results

- `tests/acceptance/test_diagnostic_scan_v1513.py`: **23 passed**.
- `tests/acceptance/test_run_validation_cycle_cli.py` +
  `test_review_only_daily_cycle.py` + `test_market_hours_gate.py` +
  `test_dte_window_chain_retrieval.py` +
  `test_opportunity_evaluation_timestamp.py` +
  `test_diagnostic_scan_v1513.py` + `tests/unit/workflows/` +
  `tests/unit/portfolio/` together: **431 passed, 7 failed** -- all 7
  failures are within the known pre-existing date-rot set (§S).
- `tests/unit/workflows/test_candidate_generation.py` +
  `tests/unit/workflows/test_candidate_funnel_equivalence.py` +
  `tests/unit/portfolio/ -k opportunity` (the two modified library
  files' own direct tests): **15 passed**.
- `tests/unit/validation/test_freeze.py` (after the manifest
  regeneration): **94 passed**.

## S. Full-suite results

**3834 passed, 6 skipped, 9 failed**, in ~81 seconds (baseline before
any change: 3811 passed, 6 skipped, 9 failed -- the +23 passing count
is exactly this release's new acceptance suite; the 9 failures are
unchanged in count).

## T. Confirmation of zero new regressions

The same 9 node IDs known since V1.5.7 through V1.5.12, unchanged in
count, identity, and root cause (the fixed-calendar-date
`tests/unit/review/conftest.py::EXPIRATION` fixture drifting outside
the `[20, 45]` DTE window as real wall-clock time advances):

1. `tests/acceptance/test_market_hours_gate.py::TestBackendPostInsideSession::test_reaches_the_mocked_runner_path`
2. `tests/acceptance/test_market_hours_gate.py::TestCliInsideSession::test_reaches_the_mocked_normal_path`
3. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate`
4. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_control_cycle_record_shows_zero_silent_generation_exceptions`
5. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`
6. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`
7. `tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses::test_tradier_production_reaches_the_mutating_cycle`
8. `tests/unit/dashboard/test_frontend_control_center.py::TestValidationEndpointIdempotencyUnaffected::test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op`
9. `tests/unit/dashboard/test_operator_status.py::TestValidationCycleRunRoute::test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome`

Identical node IDs, identical count (9), identical root cause as the
V1.5.12 freeze report's own §I. **Zero new failures anywhere in the
full suite.**

## U. `make verify-freeze` result

`portfolio_module_hash` and `run_validation_cycle_script_hash` were
reported DRIFTED before the manifest regeneration -- expected and
correct, since `src/portfolio/opportunity_scan.py` and
`scripts/run_validation_cycle.py` are exactly the two production files
this release's new capability touches. After `make freeze-manifest`:
passed cleanly against the regenerated V1.5.13 manifest:
`PAPER_TRADING_V1.5.13 / SOFTWARE FREEZE VERIFIED`.

## V. Implementation commit / freeze commit / push / tag

Pending -- created immediately after this report (see final chat
report for hashes and push/tag results).

## W. Explicit confirmation -- did NOT

- run an official validation cycle
- run `--diagnostic-scan` against Tradier production (every test in
  this release uses fakes/fixtures only)
- mutate the active cohort/database
- change NAV (remains $100,000)
- alter Risk/Quant/liquidity/DTE thresholds
- change universe, enabled strategies, or candidate ranking
- confirm a candidate or create a `ReviewedCandidate`
- place or fill a PaperBroker order
- change `TradeProposal.proposal_id`'s `max_length=64` or
  `_next_id()`'s own uniqueness scheme
- change the V1.5.12 `proposal_id_prefix` fix
- weaken Pydantic validation, Risk/Quant decision logic, or any
  existing liquidity/DTE/sizing/ranking behavior
- touch `data/options_agent.db` (confirmed: the file does not exist in
  this development sandbox, and no test in this release writes to any
  path resembling it -- every test uses a `tmp_path`-scoped sqlite
  file)

## X. Remaining risks / recommended follow-up

- The human operator must now run the first real
  `--diagnostic-scan` against live Tradier production manually, as
  explicitly instructed, to confirm the V1.5.12 proposal-id fix holds
  under real market data for PUT_CREDIT_SPREAD. This release's own
  test suite proves the mechanism is correct and safe against
  realistic fixtures, but has deliberately never executed against a
  real Tradier endpoint.
- If live PUT_CREDIT_SPREAD liquidity/pricing on the day of the first
  real diagnostic run does not clear Quant/Risk's own economics (as
  this release's own §P investigation shows can legitimately happen
  even for a well-formed candidate), that is itself a valid, expected
  diagnostic outcome -- not a sign the fix failed. The operator should
  check the printed `generation exceptions` count and any
  `ValidationError` text specifically to confirm the V1.5.12 defect
  itself is gone, independent of whether a candidate happens to clear
  Risk that day.
- The 9 known date-rot test failures remain open by design, unrelated
  to this release.
- Diagnostics for a schema-validation generation exception still
  record only the bounded exception-class name in
  `FunnelDiagnostics`/`CandidateFunnel` (official, persisted path,
  unchanged by design); the new `on_generation_exception` sanitized-
  message observability added here is diagnostic-print-only and does
  not change that persisted schema, per the task's own explicit scope
  guard.
