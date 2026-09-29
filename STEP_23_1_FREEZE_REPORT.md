# STEP_23_1_FREEZE_REPORT.md

## Experiment-Version Metadata Foundation — PAPER_TRADING_V1.5.0

This report documents Step 23-1, the first V1.5 development step:
`src.validation.experiment_version.ExperimentVersion`, a deterministic,
content-addressed record identifying the exact experimental
configuration behind future paper-trading records, plus its optional
wiring onto `ControlCycleRecord`, `TradeRecord`, `DailySnapshot`, and
`StrategyVersionManifest`. It follows the same two-commit freeze
pattern and "preserve, never overwrite, prior frozen artifacts"
discipline every prior freeze report (V1.0 through V1.4.8)
established.

**This is a metadata/auditability step only.** It changes no candidate
generation, ticker universe, active strategy set, Quant calculation,
Risk decision or threshold, position sizing, lifecycle logic, Tradier
behavior, PaperBroker behavior, human-confirmation requirement,
dashboard execution behavior, validation-cycle behavior, or
market-hours behavior, and it does not touch, initialize, or modify
the current validation cohort in any way.

## 1. Executive Summary

- **What was built:** a new, frozen `ExperimentVersion` dataclass
  (`src/validation/experiment_version.py`) carrying `version_id`,
  `software_freeze_version`, `universe_config_hash`,
  `strategy_activation_stage`, `risk_config_hash`,
  `validation_config_hash`, `market_data_config_hash`, and
  `recorded_at`. `version_id` is a SHA-256 digest over a canonical
  (sorted-key, whitespace-free) JSON encoding of every field except
  `recorded_at` — two identically-configured builds always produce the
  same `version_id` regardless of when each was built; only
  `recorded_at` itself, never the identity, depends on wall-clock time.
  This is deliberately not a random UUID.
- **Secrets never participate in identity.** `market_data_config_hash`
  is computed from `provider` name + `is_production` boolean only
  (`compute_market_data_config_hash`); the module never imports
  `os`, `dotenv`, `src.data.factory`, or any provider module, and never
  reads an environment variable or `.env` file — verified both by
  direct code inspection and by a dedicated structural test
  (`TestNoSecrets`, Section 7).
- **Backward compatibility (hard acceptance criterion).** A new,
  optional, defaulted `experiment_version_id: str | None = None` was
  added to `ControlCycleRecord`, `TradeRecord`, `DailySnapshot`, and
  (as `StrategyVersionManifest.experiment_version_id`, mirroring the
  established `cohort_label` precedent exactly) the cohort manifest.
  Every addition is a trailing, defaulted field — old JSON-blob records
  and old pydantic payloads missing the key continue to deserialize
  unchanged, with the field resolving to `None`, never fabricated or
  backfilled. Proven by `TestBackwardCompatibility` (5 tests) against
  fixtures that omit the field entirely.
- **Persistence.** A new append-only `experiment_versions` table was
  added to the existing `SqliteValidationStore`/`InMemoryValidationStore`/
  `ValidationStore` trio, using the established `_APPEND_ONLY_TABLES`
  pattern (content-addressed, `INSERT OR IGNORE`-idempotent on
  `version_id`). **No `ALTER TABLE`, no destructive migration, and no
  schema-version bump anywhere** — every change is additive and the
  existing `*_SCHEMA_VERSION` constants remain exactly as before,
  since no on-disk table shape became incompatible with an older
  reader (documented explicitly in code comments per the task's "bump
  only where technically required" instruction).
- **No behavioral wiring.** No script, orchestrator, or daily-cycle
  code path was changed to construct or attach an `ExperimentVersion`
  this step. Every test in `test_experiment_version.py` exercises the
  new model and stores directly, against `tmp_path`-scoped fixture
  configs and `tmp_path`-scoped SQLite databases — never
  `data/options_agent.db`, never the real `config/*.yaml` files, never
  a real validation cycle.
- **Re-frozen as `PAPER_TRADING_V1.5.0`.** Three new individual
  whole-file hashes were added to the manifest
  (`experiment_version_module_hash`, `validation_protocol_module_hash`,
  `validation_session_module_hash`) — deliberately **file** hashes, not
  a `src/validation/` directory hash, because `freeze.py` itself lives
  inside `src/validation/` and a directory hash would self-reference on
  every future version bump (every `FREEZE_NAME` edit would then always
  show that hash as "drifted"). All **73 of 73 checks pass**, 70
  carried unchanged from V1.4.8.
- **90-day validation was NOT started, reset, altered, or touched from
  this sandbox.** The official cohort
  (`paper-trading-v1.4.3-validation-2026-09-22`) is exactly as it was
  before this step. No official mutating validation cycle and no
  `confirm_candidate` call were executed anywhere in this session.
  `data/options_agent.db` does not exist in this sandbox, before or
  after this step.

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.4.8`, implementation commit
  `913d96e`, freeze-artifacts commit `f33d8b5` (`HEAD` at the start of
  this step). `git status --short` was clean; `git branch --show-current`
  confirmed `claude/options-trading-agent-2b4yi8`.
- 90-day validation cohort `paper-trading-v1.4.3-validation-2026-09-22`
  exists only on the operator's own real machine — no such database
  exists in this sandbox at any point in this step.
- Pre-implementation integrity baselines recorded (Section 11):
  `data/options_agent.db` absent; SHA-256 of all 4 protected config
  files.

## 3. Source Verification (Planning Assumptions vs. Actual Source)

Every relevant type/module was read directly before any edit — no
assumption was taken on faith:

- `src/validation/protocol.py`: `StrategyVersionManifest` confirmed to
  already carry `config_file_hashes: dict[str, str]` (generic) and
  `cohort_label: str = "default"` (a Step 19A precedent for exactly
  this "additive optional field on a `BaseModel`" pattern) —
  `experiment_version_id` was added to mirror `cohort_label` precisely.
  `DEFAULT_MANIFEST_CONFIG_PATHS` (3-tuple, excludes `universe.yaml`)
  confirmed and deliberately left untouched — extending it was outside
  this step's scope and would have changed manifest-building behavior.
- `src/validation/session.py`: confirmed `DailySnapshot` uses a
  hand-written `_snapshot_to_dict`/`_snapshot_from_dict` pair built on
  `dataclasses.asdict()` + `TypeName(**d)` construction — a new
  trailing defaulted field is safely omitted by an old dict. Confirmed
  the `_APPEND_ONLY_TABLES` dict + `SqliteValidationStore.__init__`/
  `_insert`/`_all` generic helpers are the established, minimal way to
  add a new append-only record type. Confirmed `ValidationStore` is an
  ABC with `InMemoryValidationStore`/`SqliteValidationStore`
  concrete implementations.
- `src/backtest/simulator.py::TradeRecord`: confirmed both real
  construction sites (`src/backtest/engine.py`, ~lines 243 and 295) use
  full keyword arguments — a trailing defaulted field is additive-safe.
- `src/portfolio/cycle_record.py::ControlCycleRecord`: confirmed
  `model_config = ConfigDict(extra="forbid", frozen=True)` — verified
  via direct pydantic behavior that `extra="forbid"` rejects **unknown**
  keys on input but is silent (fills defaults) for keys **absent** from
  input, so a new optional field is backward-compatible.
- `src/validation/freeze.py`: confirmed **zero** existing freeze
  coverage for `src/validation/` or `src/backtest/` (grepped
  `_CODE_MODULE_FILES`/`_CODE_MODULE_DIRS`) before adding any. Confirmed
  the established two-commit freeze workflow and `compute_file_hash`
  as the single shared hashing primitive (also reused directly by
  `experiment_version.py` for config hashing — no new hashing logic was
  invented).
- Confirmed via repository-wide grep: **zero** `ALTER TABLE` statements
  exist anywhere in `src/` — this repo's backward-compatibility
  guarantee is entirely "old JSON/dict rows are read by a schema with
  more optional fields," never in-place migration.

**No planning assumption was found to be wrong; no STOP condition was
triggered.**

## 4. Design Implemented

```python
@dataclass(frozen=True)
class ExperimentVersion:
    version_id: str                    # sha256, excludes recorded_at
    software_freeze_version: str       # e.g. "PAPER_TRADING_V1.5.0"
    universe_config_hash: str          # compute_file_hash(universe.yaml)
    strategy_activation_stage: str     # caller-supplied label
    risk_config_hash: str              # compute_file_hash(risk_limits.yaml)
    validation_config_hash: str        # compute_file_hash(validation.yaml)
    market_data_config_hash: str       # sha256(provider + is_production)
    recorded_at: datetime              # tz-aware; excluded from identity
```

`compute_experiment_version_id(...)` builds a `dict` of every field
except `recorded_at`, serializes it via
`json.dumps(identity, sort_keys=True, separators=(",", ":"))`, and
SHA-256-hexdigests the result. `build_experiment_version(...)` is the
convenience constructor: reads the three config files via the existing
`src.validation.protocol.compute_file_hash`, computes the market-data
hash, computes the identity, and returns the frozen record. Default
config paths point at the real `config/*.yaml` files but every path is
overridable — every test in this step passes `tmp_path`-scoped fixture
files instead.

**Cohort-manifest design decision (Option A, chosen over B/C):**
`StrategyVersionManifest` was extended with
`experiment_version_id: str | None = None`, mirroring `cohort_label`'s
exact existing precedent, rather than duplicating a field onto
`CohortRecord` — `CohortRecord` already wraps `.manifest`, so no second
source of truth was introduced.

## 5. Files Changed

| File | Change |
|---|---|
| `src/validation/experiment_version.py` | **New.** `ExperimentVersion`, `compute_market_data_config_hash`, `compute_experiment_version_id`, `build_experiment_version`. Never imports a provider module, `os`, or `dotenv`. |
| `src/validation/protocol.py` | `StrategyVersionManifest` gains `experiment_version_id: str \| None = None`; `build_validation_manifest(...)` gains a matching passthrough kwarg. `DEFAULT_MANIFEST_CONFIG_PATHS` untouched. |
| `src/validation/session.py` | `DailySnapshot` gains trailing `experiment_version_id: str \| None = None`; new `experiment_versions` table via `_APPEND_ONLY_TABLES`; `record_experiment_version`/`get_experiment_version` added to `ValidationStore`/`InMemoryValidationStore`/`SqliteValidationStore`. |
| `src/backtest/simulator.py` | `TradeRecord` gains trailing `experiment_version_id: str \| None = None` (always `None` for an ordinary backtest; exists so `src.validation.session`'s reuse of this dataclass for a live cohort's closed trades can carry it). |
| `src/portfolio/cycle_record.py` | `ControlCycleRecord` gains trailing `experiment_version_id: str \| None = None`. |
| `src/validation/freeze.py` | `FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped `1.4.8` → `1.5.0`; 3 new individual file hashes added (`experiment_version_module_hash`, `validation_protocol_module_hash`, `validation_session_module_hash`); historical comment block extended. |
| `tests/unit/validation/test_experiment_version.py` | **New.** 29 tests: determinism/hashing (8, covering all 7 required scenarios), no-secrets (4), backward-compatibility (5), new-shaped round-trip (5), persistence (4), plus supporting fixtures. |
| `tests/unit/validation/test_freeze.py` | Version assertions bumped to `1.5.0`/`PAPER_TRADING_V1.5.0`; 3 new hash-length assertions; 3 new tampering-detection tests for the new hash fields. |

No file under `src/strategies/`, `src/quant/`, `src/risk/`,
`src/lifecycle/`, `src/brokers/paper.py`, `src/brokers/fidelity.py`,
`src/llm/`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/portfolio/opportunity_scan.py`,
`src/review/confirmation.py`, `src/orchestration/pipeline.py`,
`src/dashboard/`, or any `config/*.yaml` was touched — verified
explicitly in Section 8.

## 6. Persistence Changes

- **New table:** `experiment_versions` (append-only, keyed by
  `version_id`), created only via `CREATE TABLE IF NOT EXISTS` inside
  `SqliteValidationStore.__init__`, exactly like every other table in
  this store. It is created only in `tmp_path`-scoped test databases in
  this step — it has never been created against
  `data/options_agent.db`, which does not exist in this sandbox at all.
- **No `ALTER TABLE`.** Confirmed zero `ALTER TABLE` statements were
  added anywhere (repository-wide grep still returns zero after this
  step's changes).
- **No schema-version bump.** Neither `session.py`'s validation-store
  schema-version constant nor `portfolio/persistence.py`'s
  `CONTROL_LOOP_DATABASE_SCHEMA_VERSION` was bumped — every change this
  step is either a new, independently-versioned append-only table or a
  trailing optional field on an existing model/dataclass, neither of
  which makes an older database file structurally incompatible with a
  newer reader (the established, quoted standard for when a bump is
  required). This reasoning is documented directly in code comments at
  each change site.
- **No existing JSON blob was rewritten.** No historical row in any
  table was read, modified, or re-written by this step's code.

## 7. Backward-Compatibility Results

`TestBackwardCompatibility` (5 tests, all passing) constructs
old-shaped fixtures — dicts/JSON payloads that omit
`experiment_version_id` entirely — for `ControlCycleRecord`,
`TradeRecord`, `DailySnapshot`, and `StrategyVersionManifest`, and
asserts each deserializes successfully with
`experiment_version_id is None`, never a fabricated value. A meta-test
also confirms the pre-existing, unmodified
`tests/unit/validation/conftest.py::_trade()` helper (used throughout
the existing validation test suite) continues to produce a valid
`TradeRecord` unchanged. `TestNewShapedRoundTrip` (5 tests) proves the
new field survives a full serialize → deserialize round trip when
explicitly set, for all 4 record types.

```
pytest -q tests/unit/validation/test_experiment_version.py::TestBackwardCompatibility \
          tests/unit/validation/test_experiment_version.py::TestNewShapedRoundTrip
10 passed
```

## 8. Determinism / Hashing Test Results

All 7 numbered scenarios the task required, plus one bonus pure-function
test, in `TestDeterminism` (8 tests):

1. Same configuration → same `version_id` — **PASS**
2. Changed universe config → changed `version_id` — **PASS**
3. Changed risk config → changed `version_id` — **PASS**
4. Changed validation config → changed `version_id` — **PASS**
5. Changed `strategy_activation_stage` → changed `version_id` — **PASS**
6. `recorded_at` alone does **not** change `version_id` — **PASS**
7. Secrets/API tokens do **not** participate in identity (a
   `market_data_provider`/`is_production` pair that is identical
   produces an identical hash regardless of any token value, because no
   token is ever read) — **PASS**, reinforced by `TestNoSecrets`'
   structural import-scan proof (below).

`TestNoSecrets` (4 tests): the manifest/module never contains the
substrings `api_key`/`password`/`secret`/`token`/`credential`; a
structural, import-statement-scoped regex proves
`experiment_version.py` never imports `src.data.factory`,
`src.data.tradier_provider`, `dotenv`, or `os` (the established
precedent from `tests/unit/dashboard/test_operator_status.py`, reused
here to avoid false positives from a docstring merely naming what the
module avoids).

```
pytest -q tests/unit/validation/test_experiment_version.py::TestDeterminism \
          tests/unit/validation/test_experiment_version.py::TestNoSecrets
12 passed
```

## 9. Narrow Test Results

```
pytest -q tests/unit/validation/test_experiment_version.py
29 passed

pytest -q tests/unit/validation/test_freeze.py
70 passed
```

## 10. Broader Persistence / Validation / Backtest / Portfolio Results

```
pytest -q tests/unit/validation/ tests/unit/backtest/ tests/unit/portfolio/
642 passed
```

## 11. Full Test-Suite Results

```
pytest -q
3552 passed, 6 skipped, 2 warnings
```

(V1.4.8 was 3520 passed, 6 skipped — net new: 32 tests: 29 in
`test_experiment_version.py`, 3 new tampering-detection tests in
`test_freeze.py`.)

## 12. Freeze Verification Results

```
make freeze-manifest
Wrote /home/user/Options/VALIDATION_MANIFEST.json
(manifest_hash=f7e2c8412f7042a17e83cf08c0e4956f94ee5c0989c87100a0682a1006acfa9d)

make verify-freeze
[... 73 checks ...]
PAPER_TRADING_V1.5.0 / FREEZE VERIFIED / VALIDATION NOT STARTED /
READY FOR FINAL PRE-VALIDATION ACCEPTANCE
```

**73 of 73 checks passing**, including the 3 new
`experiment_version_module_hash`/`validation_protocol_module_hash`/
`validation_session_module_hash` checks and every carried-forward
V1.4.8 check (config hashes, prompt hashes, `claude_md_hash`, every
module/script hash, every negative-capability structural check, every
safety-flag check).

## 13. Operational DB Integrity

| | Before | After |
|---|---|---|
| `data/options_agent.db` exists | No | No |
| Size | n/a | n/a |
| SHA-256 | n/a | n/a |

**Exact match: Y (both absent).** This sandbox has never contained the
operator's real database — it lives only on the operator's own
machine, which this step never accessed. No test in this step ever
opens, copies, or writes to a path named `data/options_agent.db`; every
SQLite store in `test_experiment_version.py`'s `TestPersistence` class
uses `tmp_path / "step1_test.db"`, a fresh throwaway file per test.
`rm -rf data` was run before and after every test/manual exercise this
step, per the established sandbox-artifact-avoidance convention.

## 14. Protected Config Integrity

| File | Before (SHA-256) | After (SHA-256) | Match |
|---|---|---|---|
| `config/universe.yaml` | `b88aac45f5d0eb15a974f228f12257d16d70153611767cf63ae5e5f3f26dd30c` | `b88aac45f5d0eb15a974f228f12257d16d70153611767cf63ae5e5f3f26dd30c` | Y |
| `config/risk_limits.yaml` | `e502d64800785ffd9d21980f250f9822ac287850a798d6e935d26acd4894c8b9` | `e502d64800785ffd9d21980f250f9822ac287850a798d6e935d26acd4894c8b9` | Y |
| `config/validation.yaml` | `d5f3bfe9eb951362bd050d20a6f08301f5db7707689e72c932a0801732445f52` | `d5f3bfe9eb951362bd050d20a6f08301f5db7707689e72c932a0801732445f52` | Y |
| `config/brokers.yaml` | `99a3d9dddb0ca1c8a425ac0bee6f18cf4ee0c468e451f0689450720bb5b9fed9` | `99a3d9dddb0ca1c8a425ac0bee6f18cf4ee0c468e451f0689450720bb5b9fed9` | Y |

**All 4 protected configs byte-for-byte unchanged.** Also confirmed by
`make verify-freeze`'s own `config_hash:*` checks (Section 12), all
reporting "unchanged."

## 15. Network / Execution Safety

- **No Tradier call** anywhere in this step — `experiment_version.py`
  never imports `src.data.tradier_provider` or any provider module
  (Section 8's structural proof); this sandbox has no Tradier
  credentials configured at all.
- **No Fidelity call** — `src/brokers/fidelity.py` was not read or
  written by this step.
- **No IBKR call** — `src/brokers/ibkr.py` was not touched.
- **No validation cycle was run** — `scripts/run_validation_cycle.py`
  was not executed anywhere in this step (confirmed by `git diff`
  showing it untouched, and by no test in this step invoking it).
- **No candidate confirmation occurred** — `scripts/confirm_candidate.py`
  and `src/review/confirmation.py` were not touched or executed.
- **No PaperBroker position was opened** — `src/brokers/paper.py` was
  not touched.
- **No operational cohort was modified, initialized, or reset** —
  `start_new_cohort` was not imported or called anywhere in this step;
  no cohort database exists in this sandbox.

## 16. Experimental Behavior

**No trading or validation-decision behavior changed.** No file under
`src/quant/`, `src/risk/`, `src/strategies/`, `src/lifecycle/`, or
`src/portfolio/control_loop.py`/`orchestrator.py`/`opportunity_scan.py`
was touched (Section 5's file list; confirmed empty diff below). The
new `experiment_version_id` field is optional, defaulted to `None`
everywhere it was added, and is never read, checked, or branched on by
any existing decision path — it exists purely as a place a later V1.5
step could choose to attach an identity, and nothing in this step wires
it into any live code path.

```
git diff --stat f33d8b5 -- src/strategies/ src/quant/ src/risk/ \
  src/lifecycle/ src/brokers/paper.py src/brokers/fidelity.py src/llm/ \
  config/risk_limits.yaml config/brokers.yaml config/validation.yaml \
  config/universe.yaml src/portfolio/orchestrator.py \
  src/portfolio/control_loop.py src/portfolio/opportunity_scan.py \
  src/review/confirmation.py src/orchestration/pipeline.py \
  src/dashboard/

(empty -- zero diff)
```

## 17. Current V1.4.x Validation

The existing operational cohort
(`paper-trading-v1.4.3-validation-2026-09-22`, started 2026-09-22,
$100,000 starting NAV/cash) remains **exactly as it was** before this
step, on the operator's own real machine. Nothing in this sandbox
read, wrote, or otherwise interacted with that cohort's database — it
does not exist here. No historical record (Sep 22-onward) was
backfilled with an `experiment_version_id`; every existing record, when
eventually read by V1.5-shaped code, will simply resolve the new field
to `None` — honestly reflecting that no experiment-version tracking
existed when those records were made.

## 18. Deferred Items (V1.5 Steps 2+, NOT implemented)

- Wiring `ExperimentVersion` construction into the live daily
  validation cycle or `run_control_cycle`.
- Attaching an `ExperimentVersion` to the current or any future
  cohort's `CohortRecord`/`StrategyVersionManifest` in practice
  (only the field to hold a reference was added — no cohort was
  touched).
- Market-hours gating (mentioned in the task only as an example of
  "any other V1.5 feature" not to build this step).
- Any other item from the V1.5 architecture plan beyond this
  metadata foundation.

## FREEZE

`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.8`/`1.4.8` to `PAPER_TRADING_V1.5.0`/`1.5.0`.

- **Manifest hash:** `f7e2c8412f7042a17e83cf08c0e4956f94ee5c0989c87100a0682a1006acfa9d`
- **`git_commit` recorded in the manifest:** `13510a4c56d6b90f0fd874997d557d5676c8e021` (the implementation commit).
- **`repository_state` recorded in the manifest:** `clean`.

## Software Freeze State vs. Operational Cohort State

- **Software freeze state (what this report certifies):**
  **PAPER_TRADING_V1.5.0: FROZEN.** All 73 `make verify-freeze` checks
  pass against a clean working tree at the implementation commit.
- **Operational validation-cohort state (a live, real-world fact,
  unrelated to and unaffected by this software freeze event):**
  **90_DAY_VALIDATION: IN_PROGRESS / ACTIVE.** Cohort
  `paper-trading-v1.4.3-validation-2026-09-22` continues uninterrupted
  across this metadata-foundation step — nothing in this step started a
  new cohort, reset the existing one, or touched any of its historical
  records.

**LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY. NEW_POSITION_EXECUTION: HUMAN_CONFIRMED_REVIEW_ONLY.**

- No new cohort was created anywhere in this step.
- No existing cohort database was overwritten, reset, or altered.
- No official, state-mutating validation cycle was executed anywhere in
  this session. No `confirm_candidate` call was executed against the
  official cohort anywhere in this session.
- No order/trading endpoint was called anywhere in this session.
- `data/options_agent.db` does not exist in this sandbox at the time of
  this report.
- No Risk/Lifecycle/Quant bypass exists or was introduced anywhere in
  this step (Section 16 confirms zero diff in every protected
  directory).
- No security assertion was weakened anywhere in this step — no
  `make verify-freeze` check was removed or loosened; 3 were added.

## Git Commit and Tag

- **Implementation commit** (the new model, the 4 additive-field
  changes, the freeze-manifest module bump, and every new/changed test
  file): `13510a4c56d6b90f0fd874997d557d5676c8e021`.
- **Freeze-report commit** (this freeze report + regenerated
  `VALIDATION_MANIFEST.json` + `progress.md`'s Step 23-1 entry
  together) immediately follows.
- Tag `paper-trading-v1.5.0` attempted after both commits (Section 19
  of the final report covers the push/tag outcome).
- V1.0 through V1.4.8 tags and their underlying commits were not
  touched by this step.
