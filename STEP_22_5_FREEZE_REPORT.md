# STEP_22_5_FREEZE_REPORT.md

## Review-Only Operational Runtime for the 90-Day Validation — PAPER_TRADING_V1.4.4

This report documents Step 22.5: the smallest safe operational layer to
actually run the operator's already-started 90-day validation cohort
day over day, under an explicit Review-Only human-confirmation
execution boundary, and re-freezing the platform as
**PAPER_TRADING_V1.4.4**. It follows the same two-commit freeze
pattern, and the same "preserve, never overwrite, prior frozen
artifacts" discipline, that STEP_22_FREEZE_REPORT.md (V1.0) through
STEP_22_4C_FREEZE_REPORT.md (V1.4.3) already established.

**Unlike Step 22.4B/22.4C (test-only, zero production drift), this
step adds genuine new production capability**, and its module hashes
legitimately change as a result — Section 6 documents every one, and
why each change is correct.

## 1. Executive Summary

- **Problem:** no production runtime anywhere in the repository ever
  called `src.portfolio.orchestrator.run_outer_cycle` on a schedule
  (only tests did), and `PaperBroker` had no serialize/restore path,
  so a restarted process could not resume the operator's already-
  started multi-day paper account.
- **Design constraint (operator-specified, non-negotiable):** the
  unattended daily cycle must never automatically open a new
  PaperBroker position. A Risk-approved candidate is persisted as an
  immutable `AWAITING_HUMAN` review record; only a separate, human-run
  `confirm-candidate` command — which fully revalidates the exact
  candidate from scratch — may place a simulated paper order. No real
  or faked LLM review occurs anywhere on this path.
- **New production capability:** `config/universe.yaml` +
  `src.data.universe` (frozen-universe loader), `config/operations.yaml`
  + `src.portfolio.operations_config` (operational-runtime config),
  `src.portfolio.account_state` (durable `PaperAccountState`/`Portfolio`
  stores; `PaperBroker` gained two purely additive methods,
  `export_state()`/`restore_state()`), `src.review` (new package —
  `ReviewedCandidate`, `CandidateReviewStore`, and
  `src.review.confirmation.confirm_candidate`, the ONE function
  anywhere in this codebase that may call `PaperBroker.place_order` for
  a new position), `scripts/run_validation_cycle.py` (unattended daily
  runner — never fills), `scripts/confirm_candidate.py` (the one
  human-run command that may fill), two new read-only dashboard routes,
  and `src/dashboard/bootstrap.py`.
- **Architecture-boundary fix surfaced during full regression:**
  `src.data.universe` originally imported `StrategyType` from
  `src.llm.schemas` directly, tripping the pre-existing "`src.data`
  never imports `src.llm`" boundary test. Fixed by moving strategy-
  name-to-`StrategyType` resolution to
  `src.workflows.candidate_generation` and the two new scripts, which
  already depend on both layers legitimately (Section 4).
- **Tests:** 7 new acceptance tests across 2 new files, plus 6 new unit
  test files for every new module. Full repository suite: **3402
  passed, 6 skipped, 0 failed** (up from V1.4.3's 3319 passed — net new:
  83 tests, reflecting this step's real scope). Manual CLI smoke test
  against a throwaway sqlite file (Section 8).
- **Re-frozen as PAPER_TRADING_V1.4.4.** Two new structural
  `make verify-freeze` checks added. All **55 of 55 checks pass**, 13
  carried unchanged from V1.4.3's own new-in-that-step checks, plus
  every earlier step's checks, still passing.
- **90-day validation was NOT started, reset, or touched from this
  sandbox.** The cohort named in `config/operations.yaml`
  (`paper-trading-v1.4.3-validation-2026-09-22`) already exists on the
  operator's own real machine; `src.validation.cohort.start_new_cohort`
  is not imported anywhere in this step's new code (Section 5).

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.4.3`, tag `paper-trading-v1.4.3`,
  commit `48d99da499486eca0ad2bf85a19417549f4e59f5`. `make verify-freeze`
  confirmed passing (48/48 checks) against the V1.4.3 manifest before
  this step began.
- 90-day validation had already been started, on the operator's own
  machine, entirely outside this sandbox — cohort id
  `paper-trading-v1.4.3-validation-2026-09-22`, start date 2026-09-22,
  starting NAV/cash $100,000, Day-1 snapshot already recorded. This
  sandbox's own `data/` directory (git-ignored, ephemeral to this
  container) never contained that cohort or any part of it at any
  point during this step — confirmed removed/absent before every test
  run in Section 8.

## 3. Design: The Review-Only Execution Boundary

```
Daily unattended cycle (scripts/run_validation_cycle.py):
  Tradier/mock market data -> generate_candidates -> default_quant_stage
    -> evaluate_trade_proposal (Risk Engine)      [scan_and_rank_opportunities,
    -> best Risk-approved candidate ranked          UNMODIFIED]
    -> run_outer_cycle (existing positions, unmodified)
    -> save ReviewedCandidate(status=AWAITING_HUMAN)   <-- NO PaperBroker call
    -> record DailySnapshot into the EXISTING cohort

Human-invoked, separate (scripts/confirm_candidate.py <candidate-id>):
    -> load candidate; refuse if not AWAITING_HUMAN or TTL expired
    -> fetch FRESH market data
    -> default_quant_stage (recompute, unmodified)
    -> evaluate_trade_proposal (Risk Engine, RERUN against CURRENT portfolio)
    -> refuse if not APPROVE/RESIZE, or price/capital drift exceeds tolerance
    -> validate_and_build_order_request -> PaperBroker.place_order
    -> default_portfolio_update_stage -> persist Portfolio + PaperBroker state
    -> record_opportunity (terminal outcome) + confirmation audit record
```

Both scripts reuse existing, unmodified functions only —
`src.workflows.candidate_generation.generate_candidates`,
`src.orchestration.pipeline.default_quant_stage`/
`default_portfolio_update_stage`, `src.risk.engine.evaluate_trade_proposal`,
`src.brokers.order_validator.validate_and_build_order_request`,
`src.portfolio.opportunity_scan.scan_and_rank_opportunities`,
`src.portfolio.orchestrator.run_outer_cycle`. **`src.orchestration.pipeline
.run_order_pipeline` itself is never called** anywhere on this path (it
hard-requires `devils_advocate_stage`/`portfolio_manager_stage`, which
this design deliberately does not fake) —
`src.review.confirmation.confirm_candidate` calls the same underlying
functions directly, in the same order, skipping only the LLM stages.
No file under `src/llm/` is imported anywhere in this step's new code,
except `src.llm.schemas` (Section 4).

Confirmation is idempotent at three layers: cycle-level
(`control_loop_store.get_cycle_record(cycle_id)`), candidate-store-level
(`AWAITING_HUMAN` status check before any broker call), and
order-level (`SqliteIdempotencyStore` keyed by `client_order_id =
candidate_id`).

## 4. Architecture-Boundary Fix

`tests/unit/data/test_architecture_boundary.py` pre-exists this step
and asserts `src.data` never imports `src.llm` (the Market Data Layer
sits strictly beneath the Multi-Agent Layer, ARCHITECTURE.md §3/§9).
The first draft of `src.data.universe` imported `StrategyType` from
`src.llm.schemas` directly (to resolve/validate configured strategy
names and filter to the 3 strategies
`src.workflows.candidate_generation.generate_candidates` actually
implements), tripping this check during the full-suite regression run.

**Fix:** `src.data.universe.load_universe_strategies` now returns plain
strategy-name strings (e.g. `"CASH_SECURED_PUT"`), never `StrategyType`
members, and only format-validates them (a plausible Python identifier
shape) — it never imports `src.llm`. `CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES`
and `candidate_eligible_strategies` moved to
`src.workflows.candidate_generation`, which already imports
`src.llm.schemas` legitimately and is the authoritative source of
"which strategies can this module actually produce a candidate for" in
the first place. `scripts/run_validation_cycle.py` (which may freely
import `src.llm`, being a script, not part of `src.data`) resolves the
configured names to real `StrategyType` members itself
(`StrategyType[name]`, matching `config/brokers.yaml`'s own
NAME-based convention), raising a clear configuration error on an
unknown name.

Matching test moves: `tests/unit/data/test_universe.py` now asserts
string output; `TestCandidateEligibleStrategies` moved to
`tests/unit/workflows/test_candidate_generation.py`.

## 5. Explicit CLAUDE.md / Operator-Constraint Checklist

1. **Risk Engine sole authority** — `evaluate_trade_proposal` runs
   unmodified at scan time *and* is re-run unmodified at confirm time;
   nothing can reach `PaperBroker.place_order` without a fresh
   APPROVE/RESIZE against the CURRENT portfolio.
2. **No LLM computes an authoritative number** — no file in this step
   imports `src.llm` except `src.llm.schemas` (plain, immutable data
   shapes, no LLM call anywhere in them) — enforced by two independent
   proofs: `tests/acceptance/test_no_llm_in_review_only_path.py` (AST-
   based) and the new `review_only_path_never_imports_llm`
   `make verify-freeze` check (Section 7).
3. **Fidelity manual-only untouched** — `src/brokers/fidelity.py` was
   never read or written by this step.
4. **No live trading** — `BrokerEnvironment.PAPER` never changed; the
   existing `internal_paper` `AUTOMATED` capability entry in
   `config/brokers.yaml` is reused verbatim, never widened, never
   modified.
5. **Broker capability set never inferred** — capabilities load via the
   existing, unmodified `load_broker_capabilities`; no new allowed
   strategy was added or inferred anywhere.
6. **Every figure computed once, cross-checked** — confirm-time
   revalidation *recomputes* Quant/Risk from fresh data; a drift beyond
   tolerance blocks the fill rather than trusting either number.
7. **Capital preservation over return** — no limit/sizing/drawdown
   threshold was loosened or bypassed anywhere; kill-switch/drawdown
   checks re-run, unmodified, at confirm time.
8. **The daily cycle never opens a new position** — proven three ways:
   the module docstring, the behavioral acceptance test
   (`test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`),
   and the new `daily_cycle_never_calls_place_order`
   `make verify-freeze` check (a literal-call-shape scan for
   `place_order(` in `scripts/run_validation_cycle.py`, Section 7).
9. **No new validation cohort was started, no existing cohort database
   was overwritten or reset, and the $100,000 validation account was
   never touched from this sandbox** — `src.validation.cohort
   .start_new_cohort` is not imported anywhere in
   `scripts/run_validation_cycle.py`, `scripts/confirm_candidate.py`,
   or `src.portfolio.operations_config` (grep-confirmed); every test
   and the manual CLI smoke test used only temporary/throwaway sqlite
   files, never `data/options_agent.db` (Section 8); this sandbox's own
   `data/` directory was empty both before and after this step's work.
10. **Confirmation is authorization, not trade selection** —
    `scripts/confirm_candidate.py` takes exactly one positional
    argument (a candidate id) and no flag that can alter strategy,
    strikes, expiration, direction, quantity, or sizing.

## 6. Production Hash Changes — What Changed and Why It's Correct

Unlike V1.4.2/V1.4.3, this step **does** touch production code, so the
following module hashes legitimately differ from V1.4.3's recorded
values:

| Field | V1.4.3 (old) | V1.4.4 (new) | Why |
|---|---|---|---|
| `paper_broker_module_hash` | `d25c5bde4d0f74e0…` | `260de7868bc883fe…` | Two new, purely additive public methods on `PaperBroker`: `export_state()`/`restore_state()`. Zero changes to `compute_fill`, `_apply_fill`, `settle_expiration`, `_required_collateral`, `_recompute_collateral`, `place_order`, `attempt_fill`, or any other existing method (diff-reviewed). |
| `portfolio_module_hash` | `30efd3e829306e56…` | `11ead43231fe34d4…` | Two new files added to `src/portfolio/`: `account_state.py` (durable state stores) and `operations_config.py` (operational-runtime config loader). No existing file under `src/portfolio/` was modified. |
| `config_file_hashes["universe.yaml"]` | `None` (not applicable) | `b88aac45f5d0eb15…` | `config/universe.yaml` is now real, hashed config — the tradable universe was not config-driven at all before this step. |
| `config_file_hashes["operations.yaml"]` | `None` (not applicable) | `cefaffa7e7663b01…` | Brand-new config file for this step's operational runtime. |
| `review_module_hash` | *(field did not exist)* | `e031cc0a785d8a70…` | Brand-new field, hashing the brand-new `src/review/` package — the sole home of `confirm_candidate`, the one function that may call `PaperBroker.place_order` for a new position. |
| `run_validation_cycle_script_hash` | *(field did not exist)* | `7d5c5ee4b90df565…` | Brand-new field, hashing the new unattended daily-cycle script. |
| `confirm_candidate_script_hash` | *(field did not exist)* | `3dae767a65beefb3…` | Brand-new field, hashing the new human-confirmation script. |

Every other module hash carried from V1.4.3 — `quant_module_hash`,
`risk_module_hash`, `market_calendar_module_hash`,
`alpaca_provider_module_hash`, `wheel_module_hash`,
`lifecycle_module_hash`, `tradier_provider_module_hash`,
`rate_limiter_module_hash`, `quality_gate_module_hash`,
`smoke_tradier_script_hash`, `control_loop_projection_module_hash`,
`strategy_library_version`, every `config_hash:*` for
`risk_limits.yaml`/`brokers.yaml`/`validation.yaml`/`llm.yaml`, every
`prompt_hash:*`, and `claude_md_hash` — is confirmed **unchanged**
(Section 7's `make verify-freeze` output). `src/risk/`, `src/quant/`,
`src/strategies/`, `src/lifecycle/`, `src/wheel/`,
`src/portfolio/control_loop.py`, `src/portfolio/orchestrator.py`,
`src/portfolio/opportunity_scan.py`, `src/portfolio/persistence.py`,
`src/orchestration/pipeline.py`, everything under `src/llm/`,
`src/brokers/fidelity.py`, the Tradier/Alpaca providers,
`src/validation/session.py`/`cohort.py`/`protocol.py`, and
`src/dashboard/models.py` were not modified anywhere in this step.

**Known coverage gap, honestly stated:** `src/dashboard/app.py` and
`src/dashboard/schemas.py` (the two new read-only candidate routes)
are additive changes to files that are **not** individually hashed by
any existing manifest field — only `control_loop_projection_module_hash`
covers one specific dashboard file, not the whole `src/dashboard/`
package. This is a pre-existing gap in the manifest's coverage (not
introduced by this step), matching V1.4.3 and earlier — no other step
has hashed the whole `src/dashboard/` directory either. The behavioral
proof that these two new routes are read-only lives instead in
`tests/unit/dashboard/test_app_security.py`'s `TestNoExecutionShapedRoute`
(an explicit route allowlist) and
`tests/unit/dashboard/test_candidate_routes.py`'s
`TestNoWriteRouteExistsForCandidates`.

## 7. FREEZE

`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.3`/`1.4.3` to `PAPER_TRADING_V1.4.4`/`1.4.4`. Two
new manifest fields with matching `make verify-freeze` checks were
added, following this codebase's existing `_verify_*` pattern exactly
(a direct, executable proof re-checked on every `verify_freeze` run,
independent of the matching acceptance test):

- **`daily_cycle_never_calls_place_order`** —
  `_verify_daily_cycle_never_calls_place_order()` scans
  `scripts/run_validation_cycle.py` for the literal call shape
  `place_order(` (an open paren immediately after) rather than the
  bare substring `place_order`, which the script's own module
  docstring and operator-facing print statement both legitimately
  mention by name to explain this very invariant — neither is followed
  by `(`, so neither trips the check.
- **`review_only_path_never_imports_llm`** —
  `_verify_review_only_path_never_imports_llm()` is AST-based (not
  regex), scanning `scripts/run_validation_cycle.py`,
  `scripts/confirm_candidate.py`, and every module under `src/review/`
  for any `import`/`from ... import` of `src.llm` or a `src.llm.*`
  submodule, exempting only `src.llm.schemas` (Section 4's rationale).
  Fails closed (returns `False`) on a `SyntaxError` in any target file,
  so a catastrophically broken file is drift, not a crash that would
  blow past every other check `verify_freeze` still owes the caller.

Exact `make verify-freeze` output against the freeze commit (Section
9), **55 of 55 checks passing**:

```
./scripts/verify_freeze.sh
[OK  ] manifest_exists: loaded 1.4.4
[OK  ] manifest_hash_self_consistent: matches
[OK  ] config_hash:risk_limits.yaml: unchanged
[OK  ] config_hash:brokers.yaml: unchanged
[OK  ] config_hash:validation.yaml: unchanged
[OK  ] config_hash:llm.yaml: unchanged
[OK  ] config_hash:strategies.yaml: not applicable (documented)
[OK  ] config_hash:universe.yaml: unchanged
[OK  ] config_hash:operations.yaml: unchanged
[OK  ] claude_md_hash: unchanged
[OK  ] prompt_hash:devil_advocate.md: unchanged
[OK  ] prompt_hash:market_regime.md: unchanged
[OK  ] prompt_hash:opportunity_scanner.md: unchanged
[OK  ] prompt_hash:performance_auditor.md: unchanged
[OK  ] prompt_hash:portfolio_manager.md: unchanged
[OK  ] prompt_hash:risk_reviewer.md: unchanged
[OK  ] prompt_hash:strategy_analyst.md: unchanged
[OK  ] prompt_hash:strategy_research.md: unchanged
[OK  ] prompt_hash:trade_manager.md: unchanged
[OK  ] quant_module_hash: unchanged
[OK  ] risk_module_hash: unchanged
[OK  ] paper_broker_module_hash: unchanged
[OK  ] market_calendar_module_hash: unchanged
[OK  ] alpaca_provider_module_hash: unchanged
[OK  ] wheel_module_hash: unchanged
[OK  ] lifecycle_module_hash: unchanged
[OK  ] tradier_provider_module_hash: unchanged
[OK  ] rate_limiter_module_hash: unchanged
[OK  ] quality_gate_module_hash: unchanged
[OK  ] portfolio_module_hash: unchanged
[OK  ] smoke_tradier_script_hash: unchanged
[OK  ] control_loop_projection_module_hash: unchanged
[OK  ] review_module_hash: unchanged
[OK  ] run_validation_cycle_script_hash: unchanged
[OK  ] confirm_candidate_script_hash: unchanged
[OK  ] strategy_library_version: unchanged
[OK  ] database_schema_version: 1.0.0 == current 1.0.0
[OK  ] fidelity_manual_execution_only: confirmed MANUAL
[OK  ] live_trading_disabled: BrokerEnvironment has only PAPER; manifest.live_trading_enabled=False
[OK  ] automatic_fidelity_execution_disabled: False, as required
[OK  ] validation_cohort_not_started: False, as required (90-day validation has not started)
[OK  ] alpaca_market_data_only: no alpaca.trading import found anywhere in src/
[OK  ] wheel_no_live_trading_client: no live trading-client import found anywhere in src/wheel/
[OK  ] wheel_never_becomes_its_own_order_type: StrategyKind.WHEEL absent from TRADE_PROPOSAL_ELIGIBLE, as required
[OK  ] lifecycle_no_live_trading_client: no live trading-client import found anywhere in src/lifecycle/
[OK  ] lifecycle_named_policy_count: 19 named policies (>= 19, covering every StrategyKind)
[OK  ] tradier_market_data_only: no Tradier order/trading-shaped identifier found anywhere in src/, and manifest records True
[OK  ] control_loop_cannot_execute_trades: no live trading-client import and no order-submission method name found anywhere in src/portfolio/, and manifest records True
[OK  ] dashboard_cannot_execute_trades: no live trading-client import and no order-submission method name found anywhere in src/dashboard/, and manifest records True
[OK  ] orchestrator_cannot_bypass_risk_or_lifecycle: no direct src.risk.engine/src.lifecycle.engine import and no confirm_fill call found in src/portfolio/orchestrator.py, and manifest records True
[OK  ] opportunity_scan_never_outranks_risk_monitoring: RateLimitPriority.P4_OPPORTUNITY_SCANNING > P3_PENDING_TICKET_REPRICING > P0_POSITION_RISK holds, and manifest records True
[OK  ] daily_cycle_never_calls_place_order: 'place_order' not found in scripts/run_validation_cycle.py, and manifest records True
[OK  ] review_only_path_never_imports_llm: no src.llm import (other than src.llm.schemas) found in scripts/run_validation_cycle.py, scripts/confirm_candidate.py, or src/review/, and manifest records True

PAPER_TRADING_V1.4.4 / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR FINAL PRE-VALIDATION ACCEPTANCE
```

`make verify-freeze` legitimately **failed before re-freezing** (5
missing-field `pydantic.ValidationError`s against the old V1.4.3
manifest, for the 2 new boolean fields and 3 new hash fields) — exactly
as expected for a step that adds real new production capability, not
treated as an anomaly.

## 8. Manual CLI Smoke Test

Run against a throwaway sqlite file in the session scratchpad
directory — **never `data/options_agent.db`**, confirmed absent from
this sandbox both before and after every step of this section:

1. **Seeded a throwaway cohort** matching `config/operations.yaml`'s
   cohort/account id convention (never via `start_new_cohort` inside
   the operator scripts themselves — the seed script calls it directly
   as test setup, exactly like the acceptance tests do).
2. **First `python scripts/run_validation_cycle.py` run** — completed
   cleanly, exit 0, printed `degraded_mode: False`, `opportunity scan:
   no new-position candidate` (the mock market-data provider returns
   an empty contract list, so no candidate was generated this run —
   itself a trivial but real proof of zero-auto-fill), and recorded a
   daily snapshot.
3. **Second run, same day** — printed `Cycle 'validation-2026-09-22'
   already ran today -- nothing to do.`, exit 0 (idempotent no-op).
4. **`confirm_candidate.py` against an unknown id** — `OUTCOME:
   not_found`, exit 3.
5. **Seeded one `AWAITING_HUMAN` candidate directly** (Risk-APPROVE'd
   CSP on SPY) into the same throwaway db, then ran
   `confirm_candidate.py` against it. **First confirmation**: `OUTCOME:
   data_insufficient` (`ContractNotFoundError` — the mock provider's
   fresh chain has no contracts, correctly failing the mandatory
   revalidation-against-fresh-data step rather than filling against
   stale/mismatched data), exit 2. **Second confirmation on the same
   candidate**: `OUTCOME: already_resolved`, exit 3 — proving
   idempotency held even in this DATA_INSUFFICIENT case: the order was
   never placed twice (in fact never placed at all here, correctly).

This end-to-end CLI exercise, independent of the pytest suite,
confirms every documented exit code and safety message is accurate and
that the two scripts never touch the sandbox's `data/` directory.

## 9. Git Commit, Tag, and Manifest Hash

- **Implementation commit** (this step's entire change set — every new
  module/script/test file, the additive `PaperBroker`/dashboard
  changes, the `src.data.universe` architecture-boundary fix, and the
  `FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bump plus the two
  new `make verify-freeze` checks in `src/validation/freeze.py`):
  `88b0d2754b4553c725adfa62e5d89bde690325df`.
- `VALIDATION_MANIFEST.json`'s own `git_commit` field records exactly
  this SHA (manifest generated with a fully clean working tree
  immediately after this commit, before `progress.md` or this report
  were written); `repository_state` recorded as `clean`.
- **Manifest hash:** `153c0ea42471f0a1dd805ba93add70d0ab71fd494366faa70b827a92f60fcf24`.
- **Freeze-report commit** (this freeze report + regenerated
  `VALIDATION_MANIFEST.json` + `progress.md`'s Step 22.5 entry
  together) and **git tag `paper-trading-v1.4.4`** (applied to that
  same commit) immediately follow the implementation commit above in
  `git log` — one commit after it, for the same reason V1.0 through
  V1.4.3 each used two commits.
- V1.0 through V1.4.3 tags and their underlying commits were not
  touched by this step.

## 10. Targeted Test Result

```
pytest -q tests/acceptance/test_review_only_daily_cycle.py tests/acceptance/test_no_llm_in_review_only_path.py
7 passed
```

## 11. Full Test Result

```
pytest -q
3402 passed, 6 skipped, 2 warnings
```

## 12. VALIDATION

**PAPER_TRADING_V1.4.4: FROZEN. 90_DAY_VALIDATION: IN_PROGRESS** (cohort
`paper-trading-v1.4.3-validation-2026-09-22`, started 2026-09-22, on
the operator's own machine — never started, reset, or touched from
this sandbox). **LIVE_TRADING: DISABLED. FIDELITY_EXECUTION:
MANUAL_ONLY. TRADIER: MARKET_DATA_ONLY. ALPACA: MARKET_DATA_ONLY.
NEW_POSITION_EXECUTION: HUMAN_CONFIRMED_REVIEW_ONLY.**

- No new cohort was created anywhere in this step.
- No existing cohort database was overwritten or reset.
- The operator's $100,000 validation account was never touched from
  this sandbox — every test and the manual CLI smoke test used only
  temporary/throwaway sqlite files.
- No trades were generated against the real cohort.
- No scheduling was enabled (this step ships the runner scripts; the
  operator decides how/when to invoke them on their own machine).
- No path capable of automatically transmitting a real securities/
  options order exists anywhere in this step — `PaperBroker.place_order`
  is reachable from exactly one function in the whole codebase
  (`src.review.confirmation.confirm_candidate`), itself reachable only
  from the separate, human-run `scripts/confirm_candidate.py`, never
  from the unattended daily cycle (Section 5, item 8).
- No Risk/Lifecycle bypass exists or was introduced anywhere in this
  step (unchanged from V1.4.3 — `risk_module_hash`/
  `lifecycle_module_hash` confirmed unchanged in Section 6).
- No security assertion was weakened anywhere in this step — two new
  structural checks were added to `make verify-freeze` (Section 7),
  none removed or loosened.
