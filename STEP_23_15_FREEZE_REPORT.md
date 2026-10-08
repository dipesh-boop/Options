# STEP 23.15 FREEZE REPORT — PAPER_TRADING_V1.5.15

## Status

**PAPER_TRADING_V1.5.15 / SOFTWARE FREEZE VERIFIED**

Adds the Expanded-Universe Sandbox: a strictly isolated, parallel
research environment that reuses the official cohort's exact Quant/
Risk/DTE/liquidity/sizing/ranking/no-trade standards against a wider
12-symbol universe (SPY, QQQ, IWM, DIA, AAPL, MSFT, NVDA, AMZN, META,
GOOGL, JPM, XOM — the V1.5.14 feasibility study's own set), to observe
whether universe breadth alone is enough to exercise the complete
paper-trading lifecycle. The official cohort
(`paper-trading-v1.4.3-validation-2026-09-22`) remains ACTIVE,
untouched, and unreset. `config/universe.yaml` (SPY, QQQ) and the
active strategy set are unchanged. No real sandbox database was
initialized, no real market-data cycle was run, and no real candidate
was created or confirmed during development — every test runs against
temporary databases and fake providers only. No risk limit, Quant
threshold, liquidity threshold, candidate DTE policy, no-trade hurdle,
ranking logic, or PaperBroker execution behavior was changed anywhere
in this release. Starting NAV remains $100,000.

## A. Root architectural approach — reuse, not fork

Every sandbox script reuses the identical engine functions the
official cycle already uses: `src.risk.engine.evaluate_trade_proposal`
(the Risk Engine, sole authority, unmodified), `src.portfolio
.orchestrator.run_outer_cycle`/`OpportunityScanConfig` (which
internally calls `src.portfolio.opportunity_scan
.scan_and_rank_opportunities`, unmodified), `src.brokers.paper
.PaperBroker` (unmodified), and `src.review.confirmation
.confirm_candidate` (unmodified, completely unchanged — the sole
function in this codebase that may call the simulated broker's own
order-placement method for a new position, equally true for the
sandbox as for the official cohort). Only identity (cohort/account/
database paths), the universe (`config/universe_sandbox.yaml`), and
the cycle-id namespace (`sandbox-validation-...`) differ.

## B. The official-database rejection gate

`src/portfolio/sandbox_guard.py`: `assert_path_is_not_official_database`
resolves both the candidate path and the official path via
`Path.resolve()` (works correctly whether or not either file exists
yet) and raises `SandboxGuardError` on a match — catching not just the
literal relative spelling but any alias (`./data/options_agent.db`,
`data/../data/options_agent.db`, a different starting working
directory). Called as the first action of every sandbox entry point,
before any config is loaded or any store is constructed:
`scripts/run_sandbox_cycle.py`/`confirm_sandbox_candidate.py`/
`sandbox_status.py` check it at bare module-import time (the very
first statements after the two necessary imports); `scripts
/init_expanded_universe_sandbox.py` checks it as the first statement
inside `main()`, strictly before `load_sandbox_operations_config()`
is even called and therefore before any `SqliteValidationStore`/
`SqlitePortfolioStore` is constructed. `tests/acceptance
/test_sandbox_isolation.py`'s `TestItemA_OfficialDatabaseRejection`
proves this for every one of the four entry points, including a
dedicated test that redirects `SANDBOX_DATABASE_PATH` to a literal
official-path alias and asserts loading/running the script raises
before anything is created.

## C. Sandbox identity

`src/portfolio/sandbox_identity.py` — hardwired Python module-level
constants (`SANDBOX_DATABASE_PATH`, `SANDBOX_COHORT_ID`,
`SANDBOX_ACCOUNT_ID`, `SANDBOX_MANIFEST_ID`, `SANDBOX_COHORT_LABEL`,
`SANDBOX_START_DATE`, `SANDBOX_DURATION_DAYS` = 90,
`SANDBOX_STARTING_NAV` = $100,000, `SANDBOX_CYCLE_ID_PREFIX` =
`"sandbox-validation"`), deliberately NOT a YAML file an operator
could mistype/edit into aliasing the official path. Self-checks
against the guard at its own import time. `load_sandbox_operations_
config()` reads the SAME `config/operations.yaml` the official cycle
reads, read-only, for every genuinely shared production-rule knob
(confirmation TTL, price/capital drift tolerances, market-hours
buffers, `risk_data_wiring` settings, correlation parameters, default
market regime) via pydantic `model_copy`, overriding ONLY
cohort/account identity and the four database paths. No `_env`
override exists for any sandbox identity constant — confirmed via
`grep`, zero hits for `_env`/`os.environ`/`getenv` in this module.

## D. New files

- `config/universe_sandbox.yaml` — exactly the 12 required tickers,
  `strategies: [CASH_SECURED_PUT, COVERED_CALL, PUT_CREDIT_SPREAD]`
  (mirroring `config/universe.yaml`'s own current list — this release
  tests universe breadth only, never universe + strategy breadth
  together).
- `scripts/init_expanded_universe_sandbox.py` — idempotent metadata/
  state initializer. Never contacts Tradier (metadata operations
  only). Creates a real, persisted `ExperimentVersion`
  (content-addressed, via the existing, previously-unwired
  `src.validation.experiment_version.build_experiment_version`) and an
  active `CohortRecord` wrapping a `StrategyVersionManifest`. Re-
  running with the exact same identity is a no-op (prints "ALREADY
  INITIALIZED," writes nothing); an existing sandbox with a DIFFERENT
  identity (e.g. a stale manifest id from an incompatible prior run)
  fails closed rather than resetting it.
- `scripts/run_sandbox_cycle.py` — the daily cycle runner, mirroring
  `scripts/run_validation_cycle.py`'s structure and safety ordering
  exactly (provider preflight, market-hours gate before any provider
  construction, candidate-TTL sweep, lifecycle-only safety check on a
  closed gate with open positions, the same DTE-aware fetch loop, the
  same post-fetch `evaluation_as_of` discipline). Adds a purely
  additive, sandbox-only ranked-candidate audit block (see §H).
- `scripts/confirm_sandbox_candidate.py` — thin wrapper around
  `src.review.confirmation.confirm_candidate`, completely unmodified
  (see §A). Adds a sandbox-specific pre-check layer, run BEFORE
  `confirm_candidate` is ever called: the loaded candidate must belong
  to the sandbox cohort, must carry a cycle id in the sandbox
  namespace, and must be `AWAITING_HUMAN` — each failure prints a
  clear reason and exits 3 without touching `PaperBroker`. Exactly one
  positional argument; there is no zero-argument or "latest candidate"
  form.
- `scripts/sandbox_status.py` — strictly read-only (see §F).

## E. The `cycle_helpers.py` extraction and its regression

`src/portfolio/cycle_helpers.py` (new file) mechanically extracts four
previously-private helpers out of `scripts/run_validation_cycle.py`
(`line`, `expire_stale_candidates`, `fetch_existing_position_chain`,
`run_lifecycle_only_safety_check`) so the sandbox cycle runner can
reuse them rather than duplicating a second, divergent trading-engine
implementation. `run_lifecycle_only_safety_check` gained one new
parameter, `cycle_id_prefix: str = "validation"` — the official call
site passes no override, so its own cycle-id format is byte-identical
to before this extraction.

**The regression, found and fixed before this freeze.** The first
version of this extraction had `run_lifecycle_only_safety_check`
construct its own market-data provider internally, via a module-level
`from src.data.factory import get_configured_market_data_provider`
inside `cycle_helpers.py`. This silently broke every existing test
that monkeypatches the *calling script's* own
`get_configured_market_data_provider` reference — Python resolves a
bare name against the function's own defining module's globals, not
the caller's, so the helper's independent import was a second, never-
patched copy of that name. A full-suite run surfaced 32 failures
against the known 9-failure baseline before this was caught. Fixed by
making `provider` a required, caller-supplied argument; both call
sites in `scripts/run_validation_cycle.py` and both in
`scripts/run_sandbox_cycle.py` now pass
`provider=get_configured_market_data_provider()` explicitly, using
each script's own, independently-patchable reference. A dedicated
regression test (`TestSectionE_CycleHelpersProviderRegression` in
`tests/acceptance/test_sandbox_isolation.py`) poisons the real
`get_configured_market_data_provider` and proves
`run_lifecycle_only_safety_check` never calls it when a provider is
supplied, plus two structural tests asserting both runners still pass
`provider=` explicitly at both call sites and that `cycle_helpers.py`
never re-imports the factory function.

## F. `sandbox_status.py`'s read-only guarantee

If `data/options_agent_sandbox.db` does not exist, prints
`SANDBOX NOT INITIALIZED` and exits 0 — before loading any config,
constructing any store, or touching the filesystem beyond a plain
`Path.exists()` check. Once the database file exists, every read goes
through a single `mode=ro` SQLite URI connection (`sqlite3.connect(
f"{path.resolve().as_uri()}?mode=ro", uri=True)`, the same technique
`src.portfolio.account_state.load_portfolio_read_only` already
established) — never through `SqliteValidationStore`/
`SqliteCandidateReviewStore`/`SqliteControlLoopStore`/
`SqliteLifecycleStore`/`SqlitePortfolioStore`/
`SqlitePaperAccountStateStore`, every one of which runs
`CREATE TABLE IF NOT EXISTS` in its own `__init__` and is therefore
capable of writing a missing table into existence. Queries the exact
same tables/columns those store classes use internally, parsed with
the same public `from_jsonable`/`model_validate_json`/`TypeAdapter`
helpers. `TestItemB_OfficialDatabaseImmutability` proves byte-for-byte
stability of the sandbox database across a `sandbox_status.main()`
call, and that the file is never created when absent.

## G. Universe content

Verified directly against the real, checked-in files (not a copy):
`config/universe_sandbox.yaml` has exactly SPY, QQQ, IWM, DIA, AAPL,
MSFT, NVDA, AMZN, META, GOOGL, JPM, XOM, with
`strategies: [CASH_SECURED_PUT, COVERED_CALL, PUT_CREDIT_SPREAD]`.
`config/universe.yaml` is still SPY, QQQ only — confirmed both by a
dedicated test (`TestSectionG_UniverseContent`) and by
`git diff 400b66e HEAD -- config/universe.yaml` producing no output.

## H. Observability — why a candidate did not become a trade

`src.portfolio.opportunity_scan.scan_and_rank_opportunities`
(completely unmodified, shared with the official cycle) already
computes and returns, on `OpportunityScanResult.scanned`, every
candidate it ranked this cycle — not merely the one `best` candidate
either runner has ever persisted or printed before this release.
`scripts/run_sandbox_cycle.py`'s new `_print_ranked_candidate_audit`
function is a purely additive, read-only print of that already-
computed data: for every scanned candidate, ticker, strategy,
expiration, legs, `max_profit`/`max_loss`/`capital_required` (from
`QuantitativeAnalysis`), `risk_adjusted_return` (the ranking score),
`risk_decision`/`risk_reason` (the Risk Engine's own categorical
decision and message), and the scan's own top-level `no_trade_reason`
when nothing cleared the hurdle. Changes NONE of the scoring formula,
the hurdle, or any Risk Engine rule — it is strictly an observation of
existing return values.

**Deliberate scope limitation, stated explicitly per this release's
own instruction to stop and report rather than risk a shared-
architecture change.** This does not re-run `evaluate_trade_proposal`
for every scanned candidate to additionally recover
`approved_order.estimated_credit_debit` (unlike the single `best`
candidate, which both runners already reprice once to build their
durable `ReviewedCandidate`) — doing so for every candidate on every
cycle would mean extra Risk Engine calls purely for display.
`max_profit`/`max_loss`/`capital_required`/`risk_adjusted_return`/
`risk_reason` already answer "why did this candidate not become a
trade" without it. This is also print-only, captured by whatever log
file the operator redirects this script's stdout to — not a new
durable table. This codebase's existing validation/candidate-review
stores are keyed one-row-per-candidate-id or one-row-per-cohort-day;
neither shape fits "every ranked candidate from every cycle," and
inventing a new table for it would be a shared-architecture change
this release deliberately avoids. A future step could add either if
an operator finds print-only audit insufficient.

## I. Test baseline

Current pre-V1.5.15 baseline: 3933 passed, 6 skipped, 9 failed (the
known date-rot failures — none modified or fixed by this release, per
its own instruction to leave them as the known baseline unless
explicitly asked to repair them in a separate commit). Final:
**3970 passed** (3933 + 37 new, all in `tests/acceptance
/test_sandbox_isolation.py`), 6 skipped, **9 failed** (identical node
IDs, identical root cause) — zero new failures.

## J. Pre-freeze security/architecture audit — 25/25 PASS

1. **Can any sandbox entry point reach `data/options_agent.db`?** PASS
   — no. The guard (§B) runs before any store is constructed at every
   entry point; proved by `TestItemA`/`TestItemB`.
2. **Can path aliasing bypass the guard?** PASS — no.
   `Path.resolve()` canonicalizes relative, `./`-prefixed, and
   redundant-segment (`data/../data/...`) spellings identically;
   tested explicitly.
3. **Can environment variables redirect sandbox writes to the
   official DB?** PASS — no. `sandbox_identity.py` has zero `_env`/
   `os.environ`/`getenv` reads (grep-confirmed); `SANDBOX_DATABASE_PATH`
   is a plain hardwired constant with no override mechanism, and
   `load_sandbox_operations_config`'s `model_copy(update={...})`
   unconditionally sets the four storage paths and
   cohort_id/account_id AFTER `load_operations_config()` returns, so
   no environment variable affecting the shared knobs can touch them.
4. **Can the sandbox runner instantiate a real broker?** PASS — no.
   `TestItemG` greps every sandbox script for
   `Tradier`+`Broker`/`IBKR`+`Broker`/`src.brokers.fidelity`; only
   `PaperBroker` is constructed anywhere.
5. **Can sandbox confirmation instantiate a real broker?** PASS —
   same test, same result; `confirm_sandbox_candidate.py` constructs
   only `PaperBroker`.
6. **Can the daily cycle auto-confirm?** PASS — no.
   `TestItemH_HumanConfirmationRequired` proves a full sandbox cycle
   produces zero `SqliteIdempotencyStore` entries; confirmation is
   never called from `run_sandbox_cycle.py` (grep-confirmed — the
   string `confirm_candidate` does not appear in that file).
7. **Can a sandbox candidate be confirmed through the official
   path?** PASS — no. `TestItemI`'s
   `test_official_confirm_candidate_script_cannot_see_a_sandbox_candidate`
   runs the REAL, unmodified `scripts/confirm_candidate.py` against its
   own, separately-isolated database and gets `NOT_FOUND` for a real
   sandbox candidate id.
8. **Can an official candidate be confirmed through the sandbox
   path?** PASS — no, structurally: `confirm_sandbox_candidate.py`
   only ever constructs its `CandidateReviewStore` against
   `SANDBOX_DATABASE_PATH`; an id that exists only in the official
   database can never be loaded there (`get_candidate` returns
   `None`). `TestItemI`'s two cross-cohort/cross-namespace tests prove
   the wrapper's own pre-check layer additionally refuses a
   foreign-cohort or foreign-namespace candidate even when one is
   seeded directly into the sandbox store.
9. **Can `sandbox_status.py` create or mutate a database?** PASS —
   no. See §F; proved by `TestItemB`.
10. **Can the initializer reset an existing sandbox?** PASS — no.
    `TestItemJ_IdempotentInitialization` proves a second run with
    matching identity is a no-op (`created_at` unchanged) and a
    mismatched identity fails closed (exit 1, nothing written).
11. **Can the sandbox cycle collide with official cycle IDs?** PASS —
    no. `SANDBOX_CYCLE_ID_PREFIX = "sandbox-validation"`, never
    `"validation"`; `TestItemE` proves a real sandbox cycle record's
    id is namespaced and that the official bare-date form is never
    written.
12. **Can sandbox state appear in the official dashboard/store?**
    PASS — no. No sandbox file imports anything from `src.dashboard`
    (grep-confirmed); the sandbox never touches
    `load_operations_config()`'s default path.
13. **Did `config/universe.yaml` change?** PASS — no (§G,
    `git diff` empty).
14. **Did `risk_limits.yaml` change?** PASS — no (`git diff` empty).
15. **Did `validation.yaml` change?** PASS — no (`git diff` empty).
16. **Did Quant semantics change?** PASS — no;
    `git diff 400b66e HEAD -- src/quant/` is empty.
17. **Did Risk semantics change?** PASS — no;
    `git diff 400b66e HEAD -- src/risk/` is empty.
18. **Did ranking/no-trade hurdle change?** PASS — no;
    `src/portfolio/opportunity_scan.py` and `src/strategies/ranking.py`
    are both outside the diff; §H's observability addition reads
    already-computed return values only.
19. **Did DTE semantics change?** PASS — no;
    `src/workflows/candidate_generation.py` (home of
    `QuantFilterConfig`) is outside the diff.
20. **Did strategy activation change?** PASS — no;
    `config/brokers.yaml` is outside the diff.
21. **Did Tradier execution capability get introduced?** PASS — no
    (items 4/5 above; no new execution-shaped class anywhere).
22. **Did Fidelity/IBKR execution capability get introduced?** PASS —
    no (same evidence).
23. **Does the sandbox use `PaperBroker` exclusively?** PASS — yes
    (item 4 above).
24. **Does confirmation reprice/requant/rerisk before a simulated
    fill?** PASS — yes, unconditionally: `confirm_sandbox_candidate.py`
    calls the real, completely unmodified `confirm_candidate`
    (`src/review/` is outside the diff — the function's own
    revalidate-then-fill sequence, exact-expiration refresh, Quant
    rerun, Risk rerun, and price/capital-drift checks are
    byte-identical to the official path's).
25. **Is live provider use absent from development/tests?** PASS —
    yes. Every sandbox test uses `FakeSandboxProvider`
    (never a real HTTP client); `grep` for `httpx`/`requests.`/
    `aiohttp`/`urlopen` across every sandbox script returns nothing.

**The complete `git diff 400b66e HEAD --stat` outside the five brand-
new files** (`confirm_sandbox_candidate.py`, `sandbox_status.py`,
`init_expanded_universe_sandbox.py`, `run_sandbox_cycle.py`,
`sandbox_guard.py`, `sandbox_identity.py`, `cycle_helpers.py`,
`universe_sandbox.yaml`, `test_sandbox_isolation.py`) touches exactly
one existing file: `scripts/run_validation_cycle.py`, whose own 215
changed lines are the mechanical replacement of four local function
definitions with an alias-import from the new `cycle_helpers.py`
module plus the `provider=` fix from §E — no other line in that
1600-line script changed.

## K. Development constraints honored

No Tradier production call, no real sandbox database initialization,
no real market-data cycle, and no simulated order placement against
operational files occurred at any point during this development.
Every test in `tests/acceptance/test_sandbox_isolation.py` constructs
its own temporary sqlite files (via pytest's `tmp_path`) and a fake,
offline market-data provider. One stray artifact was found and
removed during this work: an earlier draft of
`test_official_confirm_candidate_script_cannot_see_a_sandbox_candidate`
omitted a monkeypatch of `src.validation.protocol.DEFAULT_CONFIG_PATH`,
so the official `scripts/confirm_candidate.py`'s own
`load_validation_config()` call fell through to the repository's real
`config/validation.yaml`, whose default `db_path` is
`data/options_agent.db` — merely constructing `SqliteValidationStore`
against that path (a store's own `CREATE TABLE IF NOT EXISTS`) created
an empty-schema file at that path in this development container. The
test now patches that path too; the stray file was deleted (confirmed
untracked and gitignored before deletion) and a pre-existing
acceptance test (`test_validation_pipeline.py
::TestNoRealValidationCohortHasEverStarted`) that independently checks
this exact invariant now passes again.

## Status

PAPER_TRADING_V1.5.15: FROZEN. 90_DAY_VALIDATION: IN_PROGRESS (cohort
`paper-trading-v1.4.3-validation-2026-09-22`, started 2026-09-22).
EXPANDED_UNIVERSE_SANDBOX: BUILT, NOT YET INITIALIZED (the operator
has not run `scripts/init_expanded_universe_sandbox.py` on their own
machine). LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY.
NEW_POSITION_EXECUTION (official and sandbox alike): HUMAN_CONFIRMED_
REVIEW_ONLY.
