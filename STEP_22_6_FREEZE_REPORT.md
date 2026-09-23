# STEP_22_6_FREEZE_REPORT.md

## Operator CLI / Preflight Safety Remediation — PAPER_TRADING_V1.4.5

This report documents Step 22.6: two operational-safety defects found
in the V1.4.4 runner (`scripts/run_validation_cycle.py`), their fix,
and re-freezing the platform as **PAPER_TRADING_V1.4.5**. It follows
the same two-commit freeze pattern, and the same "preserve, never
overwrite, prior frozen artifacts" discipline, that
STEP_22_FREEZE_REPORT.md (V1.0) through STEP_22_5_FREEZE_REPORT.md
(V1.4.4) already established.

**V1.4.5 changes operator CLI/preflight safety only. It does not
modify frozen strategy, Quant, deterministic Risk, lifecycle policy,
or trade-selection behavior.**

## 1. Executive Summary

- **Defect 1 (root cause):** `scripts/run_validation_cycle.py` had no
  CLI argument parser at all — `main()` ignored `sys.argv` entirely
  and always executed `run_validation_cycle()`. Running
  `python scripts/run_validation_cycle.py --help` therefore silently
  ran a real, state-mutating validation cycle instead of printing help
  and exiting. This is exactly what happened to the operator on
  2026-09-23.
- **Defect 2 (root cause):** `run_validation_cycle()` read
  `OPTIONS_AGENT_DATA_PROVIDER` via the existing, general-purpose
  `get_configured_market_data_provider` factory, which happily
  constructs a `mock` (or Alpaca, or IBKR) provider — that factory's
  job is "build whatever provider is configured," not "decide whether
  this configuration is acceptable for an official, state-mutating
  validation run." No policy-level check existed to reject a
  non-Tradier-production provider before the official cycle began
  mutating state.
- **Fix:** `scripts/run_validation_cycle.py` gained a real
  `argparse.ArgumentParser` (`--preflight` flag; `-h`/`--help` and any
  unrecognized argument now exit via `parser.parse_args()` itself,
  strictly before any store/provider/PaperBroker construction).
  `src/data/factory.py` gained
  `verify_official_provider_is_tradier_production()`, a new
  policy-level gate that `run_validation_cycle()` now calls
  immediately after the cohort-existence check and before constructing
  any mutating store — see Section 3 for the exact ordering.
- **Tests:** 16 new acceptance tests
  (`tests/acceptance/test_run_validation_cycle_cli.py`), 13 new unit
  tests (`tests/unit/data/test_factory.py`), 8 new freeze/drift tests
  (`tests/unit/validation/test_freeze.py`), plus fixture-only changes
  to the existing `tests/acceptance/test_review_only_daily_cycle.py`
  (test count unchanged, 3) so its `mock` default keeps passing under
  the new preflight gate. Full repository suite: **3439 passed, 6
  skipped, 0 failed** (up from V1.4.4's 3402 passed — net new: 37
  tests, exactly matching 16 + 13 + 8).
- **Re-frozen as PAPER_TRADING_V1.4.5.** Two new structural
  `make verify-freeze` checks added
  (`help_cannot_execute_validation`, `official_cycle_requires_tradier_preflight`),
  plus a new `factory_module_hash` field. All **57 of 57 checks pass**,
  every one of V1.4.4's 55 checks still passing unchanged.
- **90-day validation was NOT started, reset, altered, or touched from
  this sandbox.** The official cohort
  (`paper-trading-v1.4.3-validation-2026-09-22`) and its 2026-09-22
  and 2026-09-23 records are exactly as they were before this step —
  no code in this step reads, writes, or deletes `data/options_agent.db`,
  and `confirm_candidate`/an official mutating cycle were never
  executed anywhere in this session (Section 8).

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.4.4`, tag `paper-trading-v1.4.4`,
  commit `88b0d2754b4553c725adfa62e5d89bde690325df`.
  `make verify-freeze` confirmed passing (55/55 checks) against the
  V1.4.4 manifest before this step began.
- 90-day validation cohort `paper-trading-v1.4.3-validation-2026-09-22`
  already existed on the operator's own machine, started 2026-09-22
  ($100,000 NAV/cash, Day-1 snapshot recorded), with a second, accidental
  cycle record (`validation-2026-09-23`) created by the operator running
  `python scripts/run_validation_cycle.py --help` against V1.4.4 — that
  invocation used the `mock` provider, found no usable chains, entered
  degraded mode, generated a provider-outage alert, produced zero
  candidates/trades/fills, left NAV/cash at $100,000/$100,000, and
  recorded the 2026-09-23 $100,000 daily snapshot. Both records are
  legitimate historical audit evidence and were not read, modified, or
  deleted anywhere in this step.

## 3. Design

### 3a. CLI argument parsing (Defect 1)

`main()`'s first statement is now `args = _build_arg_parser().parse_args()`.
`_build_arg_parser()` returns a plain `argparse.ArgumentParser` with one
optional flag, `--preflight` (`store_true`). `argparse` itself calls
`sys.exit(0)` for `-h`/`--help` (after printing help text) and
`sys.exit(2)` for an unrecognized argument — both happen inside
`parse_args()`, before `main()`'s next line ever runs, so neither case
can reach `run_preflight()` or `run_validation_cycle()`. No-argument
invocation is unchanged: it still runs the official mutating cycle.
`scripts/run_validation_cycle.sh` now forwards `"$@"` so `--help`/
`--preflight` reach the Python script through the shell wrapper too.

### 3b. Provider preflight (Defect 2)

```python
def verify_official_provider_is_tradier_production(
    selection: DataProviderSelection | None = None,
    tradier_config: TradierConfig | None = None,
) -> None:
    ...  # raises OfficialProviderPreflightError unless:
         #   - selection.data_provider.lower() == "tradier", AND
         #   - a non-empty TradierConfig.token is configured, AND
         #   - TradierConfig.base_url (normalized) == the production host
```

`mock`/`alpaca`/`ibkr`/any unknown provider name, a `tradier` selection
with no token, and a `tradier` selection pointed at the sandbox host or
any other non-production URL are all rejected with a clear,
token-free error message (`test_error_message_never_contains_the_token`
proves the token itself never leaks into the exception text).
`run_validation_cycle()` calls this function immediately after the
cohort-existence check and before constructing
`SqliteControlLoopStore`/`SqlitePortfolioStore`/
`SqlitePaperAccountStateStore`/`SqliteCandidateReviewStore` or any
other mutating object — a caught `OfficialProviderPreflightError`
prints `FAIL: provider preflight -- ...` and returns `False` before any
of them exist.

### 3c. Mutation ordering (the actual sequence, as implemented)

```
main() -> parser.parse_args()                              [Defect 1's fix -- FIRST]
       -> args.preflight ? run_preflight() : run_validation_cycle()

run_preflight():
    load config (read-only)
    verify cohort exists (read-only has_cohort_started check)
    verify_official_provider_is_tradier_production()        [Defect 2's fix]
    print "PREFLIGHT ONLY -- NO VALIDATION STATE MUTATED"
    -- never constructs SqliteControlLoopStore / SqlitePortfolioStore /
       SqlitePaperAccountStateStore / SqliteCandidateReviewStore

run_validation_cycle():
    verify cohort exists (read-only has_cohort_started check)
    verify_official_provider_is_tradier_production()        [Defect 2's fix -- BEFORE any mutation]
    -- only past this point does any mutating store get constructed --
    construct stores, expire stale candidates, fetch chains,
    run_outer_cycle, save candidate, record snapshot, ...
```

This matches the user's required 7-step preflight ordering: CLI parse
(1) happens in `main()` before either branch; config load (2) and
cohort verification (3) happen first inside each branch; provider
verification (4) happens before Tradier-credential-detail checks are
even reached (the same function checks the token's presence as part of
step 4/5 combined, since `TradierConfig.token` is what "is this really
Tradier production" collapses to); only after all of that does the
mutating path (7) begin. A connectivity failure *after* this point
(e.g. a real Tradier outage once the official cycle has legitimately
begun) is unchanged from V1.4.4 — still recorded as a degraded/
data-insufficient cycle, never rejected as a preflight failure, because
by that point the provider identity itself was never in question.

### 3d. `--preflight` mode

Implemented as specified: reads config, verifies the cohort exists,
calls the same `verify_official_provider_is_tradier_production()`, and
on success prints `PREFLIGHT ONLY -- NO VALIDATION STATE MUTATED` and
exits 0. On failure (missing cohort, or provider not Tradier
production) it prints a clear failure message and exits 1. It never
constructs any of the four Sqlite stores capable of mutation, never
sweeps candidate TTLs, never records a cycle/snapshot/alert, and never
touches `PaperBroker`. Wired into
`scripts/run_validation_cycle.sh`/`make validate-preflight` for
convenience.

### 3e. Provider-name/source integrity review

Reviewed `provider_name = next((c.source for c in
chains_by_ticker.values()), "unknown")` in
`scripts/run_validation_cycle.py`. It reads `.source` off an actually
-returned `OptionChain`, which each provider's own `get_option_chain`
sets to its own canonical name (`"mock"`, `"tradier"`, ...) — it can
only ever report `"tradier"` if a chain object that really came from
`TradierMarketDataProvider` was returned, and `"unknown"` if
`chains_by_ticker` is empty (the honest degraded-mode case). No code
path lets a failed/empty fetch masquerade as a Tradier success. **No
change was required or made to this logic.**

## 4. Explicit CLAUDE.md / Operator-Constraint Checklist

1. **Risk Engine sole authority** — unchanged; this step touches no
   file under `src/risk/`.
2. **No LLM computes an authoritative number** — unchanged; this step
   adds no new import of `src.llm` anywhere.
3. **Fidelity manual-only untouched** — `src/brokers/fidelity.py` was
   never read or written by this step.
4. **No live trading** — unchanged; `BrokerEnvironment.PAPER` and
   `config/brokers.yaml` were not touched.
5. **Broker capability set never inferred** — unchanged; no capability
   file was touched.
6. **Every figure computed once, cross-checked** — unchanged; this
   step adds no new Quant/Risk number anywhere, only a categorical
   provider-identity check.
7. **Capital preservation over return** — unchanged; no limit, sizing,
   or drawdown threshold was touched.
8. **The daily cycle still never opens a new position** —
   `daily_cycle_never_calls_place_order` (V1.4.4's check) re-verified
   passing against the modified script (Section 6).
9. **No cohort was started, reset, or altered; no existing record was
   rewritten or deleted** — this step adds no code path that calls
   `start_new_cohort`, deletes/truncates any table, or writes to the
   2026-09-22 or 2026-09-23 records. `data/options_agent.db` does not
   exist in this sandbox at any point during this step (confirmed
   absent before and after every test run and every manual command in
   Section 8).
10. **`--help`/an unknown argument perform zero network/database/
    provider-construction work** — proven both behaviorally
    (`tests/acceptance/test_run_validation_cycle_cli.py`, real
    `subprocess.run` invocations against a clean `tmp_path` asserting
    no `*.db` file is created) and structurally
    (`help_cannot_execute_validation` freeze check, Section 6).
11. **An official cycle configured against `mock` (or any non-Tradier
    -production provider) is rejected before it can mutate anything** —
    proven both behaviorally (`TestMockProviderRejection` in the same
    acceptance file: no cycle record, no daily snapshot, no candidate
    expiration, no reviewed candidate, no account-state
    bootstrap-or-change, no PaperBroker order) and structurally
    (`official_cycle_requires_tradier_preflight` freeze check,
    Section 6).
12. **The `LIQUIDITY_ADJUSTED` fill model was not touched** — this
    step contains no change to `src/brokers/paper.py` at all
    (`paper_broker_module_hash` confirmed unchanged, Section 6).

## 5. Files Changed

- `src/data/factory.py` — added `OfficialProviderPreflightError`,
  `verify_official_provider_is_tradier_production()`. No existing
  function's behavior changed.
- `scripts/run_validation_cycle.py` — added `argparse`-based CLI
  parsing in `main()`, the new `run_preflight()` async function, the
  provider-preflight call inside `run_validation_cycle()`. No change
  to the cycle's actual scan/monitor/save logic once past preflight.
- `scripts/run_validation_cycle.sh` — forwards `"$@"` to the Python
  script.
- `Makefile` — added `validate-preflight` target.
- `src/validation/freeze.py` — added `factory_module` to the hashed
  file set, three new `FreezeManifest` fields
  (`factory_module_hash`, `help_cannot_execute_validation`,
  `official_cycle_requires_tradier_preflight`), the matching
  `_verify_help_cannot_execute_validation()` /
  `_verify_official_cycle_requires_tradier_preflight()` checks (plus
  the new `_extract_function_body()` helper both rely on), and the
  `PAPER_TRADING_V1.4.4`/`1.4.4` → `PAPER_TRADING_V1.4.5`/`1.4.5` bump
  (both the module-level constants and the `freeze_version="..."`
  literal inside `build_freeze_manifest`).
- `tests/acceptance/test_run_validation_cycle_cli.py` (new) — 16
  tests covering help safety, unknown-argument safety, mock-provider
  rejection, Tradier-production-provider acceptance, and
  `--preflight` mode.
- `tests/unit/data/test_factory.py` — 13 new tests for
  `verify_official_provider_is_tradier_production`.
- `tests/acceptance/test_review_only_daily_cycle.py` — fixture now
  sets `OPTIONS_AGENT_DATA_PROVIDER=tradier` +
  `OPTIONS_AGENT_TRADIER_TOKEN=fake-token-for-tests-only` so its
  pre-existing scenario (which relies on a monkeypatched
  `FakeMarketDataProvider`, not a real Tradier call) keeps passing
  under the new preflight gate.
- `tests/unit/validation/test_freeze.py` — version-string bump plus
  new assertions/tests for the three new manifest fields and their
  drift detection.

No file under `src/risk/`, `src/quant/`, `src/strategies/`,
`src/lifecycle/`, `src/wheel/`, `src/brokers/paper.py`,
`src/brokers/fidelity.py`, `src/portfolio/` (any file),
`src/review/` (any file), `src/orchestration/pipeline.py`, or
anything under `src/llm/` was modified anywhere in this step.

## 6. Production Hash Changes — What Changed and Why It's Correct

| Field | V1.4.4 (old) | V1.4.5 (new) | Why |
|---|---|---|---|
| `run_validation_cycle_script_hash` | `7d5c5ee4b90df565…` | `cfb28fc669e3b4f5…` | The script gained real CLI parsing, `run_preflight()`, and the provider-preflight call — exactly this step's intended change. |
| `factory_module_hash` | *(field did not exist)* | `6a468bb360571af6…` | Brand-new field, hashing `src/data/factory.py`, which gained `verify_official_provider_is_tradier_production`. |
| `help_cannot_execute_validation` | *(field did not exist)* | `True` | Brand-new field/check (Section 3a/7). |
| `official_cycle_requires_tradier_preflight` | *(field did not exist)* | `True` | Brand-new field/check (Section 3b/7). |

Every other hash is confirmed **unchanged**, including fields most
relevant to this step's own blast radius —
`confirm_candidate_script_hash` (`3dae767a65beefb3…`, identical),
`portfolio_module_hash` (`11ead43231fe34d4…`, identical),
`review_module_hash` (`e031cc0a785d8a70…`, identical),
`paper_broker_module_hash` (unchanged — `src/brokers/paper.py` was not
touched) — plus `quant_module_hash`, `risk_module_hash`,
`market_calendar_module_hash`, `alpaca_provider_module_hash`,
`wheel_module_hash`, `lifecycle_module_hash`,
`tradier_provider_module_hash`, `rate_limiter_module_hash`,
`quality_gate_module_hash`, `smoke_tradier_script_hash`,
`control_loop_projection_module_hash`, `strategy_library_version`,
every `config_hash:*`, every `prompt_hash:*`, and `claude_md_hash`
(Section 7's `make verify-freeze` output).

## 7. FREEZE

`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.4`/`1.4.4` to `PAPER_TRADING_V1.4.5`/`1.4.5`. Two
new manifest fields with matching `make verify-freeze` checks were
added, following this codebase's existing `_verify_*` pattern exactly
(a direct, executable proof re-checked on every `verify_freeze` run,
independent of the matching acceptance test):

- **`help_cannot_execute_validation`** —
  `_verify_help_cannot_execute_validation()` confirms
  `argparse.ArgumentParser` appears in the script and that, within
  `main()`'s own body (isolated via the new `_extract_function_body()`
  helper, so the module's own docstring text and other functions'
  `def` lines can't produce a false pass), `.parse_args(` appears
  strictly before both `run_validation_cycle()` and `run_preflight()`.
- **`official_cycle_requires_tradier_preflight`** —
  `_verify_official_cycle_requires_tradier_preflight()` confirms that,
  within `run_validation_cycle()`'s own body (same isolation
  technique, needed because `_expire_stale_candidates`'s own `def`
  line and internal `review_store.save_candidate(` call both sit
  earlier in the file and would otherwise produce a false pass),
  `verify_official_provider_is_tradier_production(` appears strictly
  before every one of five known mutating calls
  (`_expire_stale_candidates(`, `portfolio_store.save(`,
  `account_state_store.save(`, `review_store.save_candidate(`,
  `validation_store.record_snapshot(`).

Both checks were empirically validated to fail when the property they
claim is actually broken (verified by temporarily renaming the
`argparse.ArgumentParser` text, temporarily moving `.parse_args(` past
its real call site, and temporarily renaming the preflight call — each
reproduced in `tests/unit/validation/test_freeze.py`,
`TestHelpCannotExecuteValidationCheck`/
`TestOfficialCycleRequiresTradierPreflightCheck`), not just passing by
coincidence.

Exact `make verify-freeze` output against the freeze commit (Section
9), **57 of 57 checks passing**:

```
./scripts/verify_freeze.sh
[OK  ] manifest_exists: loaded 1.4.5
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
[OK  ] factory_module_hash: unchanged
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
[OK  ] help_cannot_execute_validation: scripts/run_validation_cycle.py's main() parses CLI arguments (argparse) before its first mutating call, and manifest records True
[OK  ] official_cycle_requires_tradier_preflight: scripts/run_validation_cycle.py calls verify_official_provider_is_tradier_production before every one of its known mutating calls, and manifest records True

PAPER_TRADING_V1.4.5 / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR FINAL PRE-VALIDATION ACCEPTANCE
```

`make verify-freeze` legitimately **failed before re-freezing** (3
missing-field `pydantic.ValidationError`s against the old V1.4.4
manifest, for the 3 new fields) — exactly as expected for a step that
extends the manifest schema, not treated as an anomaly.

## 8. Test Results

### 8a. `--help` safety

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py::TestHelpSafety
3 passed
```
Confirms: `--help` exits 0, prints usage/help text, never calls
`run_validation_cycle()`/`run_preflight()`, performs no provider
construction or network call, and creates no `*.db` file in a clean
working directory.

### 8b. Unknown-argument safety

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py::TestUnknownArgumentSafety
2 passed
```
Confirms: an unrecognized flag exits non-zero (2, via `argparse`),
never runs a validation cycle, and creates no `*.db` file.

### 8c. Mock-provider rejection

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py::TestMockProviderRejection
2 passed
```
Confirms: an official cycle run with `OPTIONS_AGENT_DATA_PROVIDER=mock`
(the default) exits non-zero with a clear provider-preflight error,
and creates no cycle record, no daily snapshot, no candidate
expiration, no reviewed candidate, no account-state
bootstrap-or-change, and no PaperBroker order. (A further 5 unit-level
tests, `TestOfficialProviderPreflightUnit`, exercise the same rejection
at the `verify_official_provider_is_tradier_production` level directly.)

### 8d. Tradier-production-provider path

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses tests/acceptance/test_run_validation_cycle_cli.py::TestPreflightMode
1 + 3 = 4 passed
```
Confirms: a correctly configured Tradier-production environment passes
preflight and reaches the existing (unmodified) cycle behavior;
`--preflight` reports readiness (or a specific failure) without
mutating any state.

### 8e. Focused tests (this step's full new/changed surface)

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py tests/unit/data/test_factory.py tests/acceptance/test_review_only_daily_cycle.py tests/unit/validation/test_freeze.py
16 + 23 + 3 + 61 = 103 passed
```

### 8f. Retained safety tests (unchanged, still passing)

```
pytest -q tests/acceptance/test_no_llm_in_review_only_path.py
4 passed
```
`daily_cycle_never_calls_place_order` and
`review_only_path_never_imports_llm` are re-verified passing both as
`make verify-freeze` structural checks (Section 7 output) and via
their original acceptance/unit tests, unmodified by this step. Tradier
market-data-only, Fidelity manual-only, and Risk/orchestrator
structural checks are likewise unmodified and re-confirmed passing in
the full-suite run (Section 8g).

### 8g. Full regression

```
pytest -q
3439 passed, 6 skipped, 2 warnings in 48.31s
```
(V1.4.4 was 3402 passed, 6 skipped — net new: 37 tests, matching this
step's 16 + 13 + 8 new test additions across
`tests/acceptance/test_run_validation_cycle_cli.py`,
`tests/unit/data/test_factory.py`, and
`tests/unit/validation/test_freeze.py` respectively.)

## 9. Git Commit, Tag, and Manifest Hash

- **Implementation commit** (CLI parsing, provider preflight,
  `--preflight` mode, the two new freeze checks, and every new/changed
  test file): `0ff889cab46d2d74163779ae3b41b4c2bfd034a1`.
- `VALIDATION_MANIFEST.json`'s own `git_commit` field records exactly
  this SHA (manifest generated with a fully clean working tree
  immediately after this commit, before `progress.md` or this report
  were written); `repository_state` recorded as `clean`.
- **Manifest hash:** `6ca14b630d333c30c9037d6dd9e807787f2647b9a8bad336937bb177ca51118f`.
- **Freeze-report commit** (this freeze report + regenerated
  `VALIDATION_MANIFEST.json` + `progress.md`'s Step 22.6 entry
  together) and **git tag `paper-trading-v1.4.5`** (annotated, applied
  to that same commit) immediately follow the implementation commit
  above in `git log`.
- **Branch push status / tag push status:** recorded at the end of
  this report, after both commits and the tag were created (see the
  final summary message accompanying this report).
- V1.0 through V1.4.4 tags and their underlying commits were not
  touched by this step.

## 10. VALIDATION

**PAPER_TRADING_V1.4.5: FROZEN. 90_DAY_VALIDATION: IN_PROGRESS**
(cohort `paper-trading-v1.4.3-validation-2026-09-22`, started
2026-09-22, on the operator's own machine — never started, reset, or
touched from this sandbox; its 2026-09-22 and 2026-09-23 records are
unchanged by this step). **LIVE_TRADING: DISABLED. FIDELITY_EXECUTION:
MANUAL_ONLY. TRADIER: MARKET_DATA_ONLY. ALPACA: MARKET_DATA_ONLY.
NEW_POSITION_EXECUTION: HUMAN_CONFIRMED_REVIEW_ONLY.**

- No new cohort was created anywhere in this step.
- No existing cohort database was overwritten, reset, or altered.
- No official, state-mutating validation cycle was executed anywhere
  in this session. No `confirm_candidate` call was executed against
  the official cohort anywhere in this session.
- `data/options_agent.db` does not exist in this sandbox at the time
  of this report.
- No Risk/Lifecycle/Quant bypass exists or was introduced anywhere in
  this step (`risk_module_hash`/`lifecycle_module_hash`/
  `quant_module_hash` confirmed unchanged in Section 6).
- No security assertion was weakened anywhere in this step — two new
  structural checks were added to `make verify-freeze`, none removed
  or loosened.
