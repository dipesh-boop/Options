# STEP_22_7_FREEZE_REPORT.md

## Tradier Market-Data Timestamp Semantics / Freshness Safety Hotfix — PAPER_TRADING_V1.4.6

This report documents Step 22.7: a narrow market-data correctness and
validation-safety hotfix discovered during V1.4.5's local acceptance,
and re-freezing the platform as **PAPER_TRADING_V1.4.6**. It follows
the same two-commit freeze pattern, and the same "preserve, never
overwrite, prior frozen artifacts" discipline, that
STEP_22_FREEZE_REPORT.md (V1.0) through STEP_22_6_FREEZE_REPORT.md
(V1.4.5) already established.

**V1.4.6 changes Tradier market-data timestamp handling and a shared
freshness-primitive defensive rule only. It does not modify frozen
strategy, Quant, deterministic Risk, lifecycle policy, or
trade-selection behavior, and does not loosen `DEFAULT_MAX_QUOTE_AGE`
or any other freshness threshold.**

## 1. Executive Summary

- **Root cause 1:** `src/data/tradier_provider.py` selected Tradier's
  `trade_date` (the timestamp of the security's last *printed trade*)
  as the canonical `UnderlyingQuote.timestamp`/`OptionContract.timestamp`
  before ever considering `bid_date`/`ask_date`. Tradier's `trade_date`
  can legitimately lag well behind the current market — a thinly-traded
  name, or simply no print yet this session — even while the bid/ask
  (the actual, tradable quote) is fully current. A live raw SPY quote
  captured during V1.4.5 acceptance confirmed this concretely:
  `trade_date` ≈ `2026-09-23T00:00:00.002Z` (a stale midnight print)
  vs. `bid_date`/`ask_date` ≈ `2026-09-23T11:10:1{3,5}Z` (the actual,
  current quote, over 11 hours newer). Under the old code, a currently
  quoted, tradable contract could be incorrectly treated as STALE by
  the ~15-minute freshness gate purely because the underlying hadn't
  printed a trade recently.
- **Root cause 2 (general architecture gap, not Tradier-specific):**
  `TimestampedModel.freshness_status()`/`require_fresh()` computed
  `age = as_of - timestamp` with no floor. A provider timestamp
  materially *ahead* of `as_of` (clock skew, or corrupted data) would
  produce a negative age, and `age > max_age` never fires on a negative
  number — so a corrupted future timestamp would read as FRESH
  regardless of `max_age`. `src.llm.schemas.TradeProposal` and
  `src.brokers.fidelity` already fail closed on this (both reject
  `data_timestamp`/`market_data_timestamp > timestamp` outright) — the
  shared canonical `src.data.provider.TimestampedModel` primitive
  itself, which every `UnderlyingQuote`/`OptionContract`/`OptionChain`
  inherits and which the Risk Engine's own `require_fresh` call relies
  on, did not have an equivalent guard.
- **Fix 1:** new `_select_quote_timestamp()` in `tradier_provider.py`
  prefers `max(bid_date, ask_date)` when both are present, falls
  through to whichever one is present, then to `trade_date` only when
  neither quote-side timestamp exists, and to local capture time
  (`now`) only when the provider supplies no timestamp at all. Wired
  into both `_parse_quote_json` and `_parse_option_json`.
  `bid_timestamp`/`ask_timestamp`/`trade_timestamp` on `OptionContract`
  are unaffected — they keep carrying Tradier's raw per-field values
  exactly as before.
- **Fix 2:** a new `_MAX_FUTURE_CLOCK_SKEW` (1 minute) tolerance in
  `src/data/provider.py`'s `TimestampedModel.freshness_status()`/
  `require_fresh()` — a timestamp more than 1 minute ahead of `as_of`
  is now unconditionally STALE/raises, independent of `max_age`, while
  ordinary sub-minute clock skew still reads FRESH exactly as before.
  `DEFAULT_MAX_QUOTE_AGE` is unchanged; this addition only ever makes
  an existing check *harder* to pass, never easier.
- **Tests:** 22 new tests across `tests/unit/data/
  test_tradier_provider.py` (17), `tests/unit/data/test_provider.py`
  (5), covering every case the task specification enumerates (A
  through K: trade_date-older-than-bid/ask, both-present selection,
  bid-only, ask-only, trade-date-only fallback, no-timestamp fallback,
  option-contract canonical-vs-per-side timestamps, stale-still-fails,
  fresh-despite-old-last-trade, future-timestamp handling, and
  unaffected field mapping), plus 1 new freeze-drift test
  (`test_tampering_with_the_data_provider_module_is_caught`). Full
  repository suite: **3462 passed, 6 skipped, 0 failed** (up from
  V1.4.5's 3439 passed — net new: 23 tests).
- **Re-frozen as PAPER_TRADING_V1.4.6.** `src/data/provider.py` — the
  shared `TimestampedModel` freshness primitive, touched for the first
  time in this step — was not previously covered by any
  `make verify-freeze` module hash; a new `data_provider_module_hash`
  field/check was added, mirroring the precedent set for
  `factory_module_hash` in V1.4.5. All **58 of 58 checks pass**, all
  56 checks carried unchanged from V1.4.5.
- **90-day validation was NOT started, reset, altered, or touched from
  this sandbox.** The official cohort
  (`paper-trading-v1.4.3-validation-2026-09-22`) and its 2026-09-22 and
  2026-09-23 records are exactly as they were before this step. No
  official mutating validation cycle and no `confirm_candidate` call
  against the official cohort were executed anywhere in this session.
  `data/options_agent.db` does not exist in this sandbox.
- **No Tradier credentials were available in this sandbox.** A live,
  read-only smoke test could not be performed here — deterministic
  unit/acceptance tests are the full basis for this report's
  correctness claims, exactly as the task specification allows. The
  operator's own separate real Tradier smoke test remains the final
  confirmation step.

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.4.5`, tag `paper-trading-v1.4.5`,
  commit `980b060...` (freeze-artifacts commit).
  `make verify-freeze` confirmed passing (57/57 checks) against the
  V1.4.5 manifest before this step began.
- 90-day validation cohort `paper-trading-v1.4.3-validation-2026-09-22`
  already existed, started 2026-09-22 ($100,000 NAV/cash, Day-1
  snapshot recorded), plus the 2026-09-23 accidental degraded/mock
  cycle record from the operator's earlier `--help` invocation (fixed
  in V1.4.5, preserved as legitimate historical audit evidence). Both
  records were not read, modified, or deleted anywhere in this step.
- The defect was discovered by the operator during V1.4.5's own local
  acceptance testing: a real Tradier smoke test (SPY underlying quote,
  32 expirations, 310 contracts for the nearest expiration, no
  trading/account endpoints called, validation DB counts unchanged)
  succeeded functionally, but the raw response's `trade_date`/
  `bid_date`/`ask_date` values, inspected by the operator, revealed the
  timestamp-selection defect described in Section 1.

## 3. Required Investigation (Section 2 of the task spec)

### 3a. Tradier's documented timestamp semantics

Per Tradier's own Brokerage API documentation for `/markets/quotes`
and `/markets/options/chains` (`trade_date`/`bid_date`/`ask_date`, all
reported as epoch milliseconds):

- **`trade_date`**: the timestamp of the security's **last printed
  trade**. This is not continuously updated — for a security that
  hasn't traded recently (low liquidity, or simply before the day's
  first print), it can sit arbitrarily far behind the current wall
  clock, even while the market is open and actively quoting.
- **`bid_date`**: the timestamp the **current bid** was last quoted.
  Updates continuously as the NBBO bid moves, independent of whether a
  trade has printed.
- **`ask_date`**: the timestamp the **current ask** was last quoted.
  Same continuous-update behavior as `bid_date`, independently.

This matches exactly what the live raw quote captured during V1.4.5
acceptance showed: `trade_date` frozen at a stale midnight print while
`bid_date`/`ask_date` tracked the live, current market over 11 hours
later. Documented here and in `_select_quote_timestamp`'s own
docstring in `src/data/tradier_provider.py`.

### 3b. Consumers inspected

- `UnderlyingQuote.timestamp` / `OptionContract.timestamp`: read by
  `TimestampedModel.age`/`freshness_status`/`require_fresh` (the sole
  freshness mechanism), which is in turn called directly by
  `src.risk.trade_risk.require_fresh_contract`,
  `src.risk.engine`'s `market_data.underlying.require_fresh`,
  `src.brokers.paper.PaperBroker` (`update_market_data`'s fill-time
  freshness check), `src.workflows.candidate_generation`
  (`chain.freshness_status`), `src.workflows.feed_health`
  (`FreshnessResult`), and `src.portfolio.revaluation`
  (`contract.freshness_status`). Every one of these routes through the
  now-fixed canonical timestamp with zero changes to their own code.
- `OptionContract.bid_timestamp`/`ask_timestamp`/`trade_timestamp`:
  grepped repo-wide — no consumer outside `src/data/option_chain.py`/
  `src/data/tradier_provider.py` itself exists yet. These fields are
  purely provenance data at present; left completely untouched by this
  fix (Requirement 2: "Keep these fields intact").
- `freshness_status()`/`require_fresh()`/data quality gates: the sole
  home of the freshness computation is `TimestampedModel` in
  `src/data/provider.py`; `src.data.quality_gate.validate_underlying_quote`/
  `validate_option_contract` call `freshness_status` directly and
  needed no changes — the fix operates entirely upstream of them, at
  the point the canonical `.timestamp` is first assigned/validated.
- **Two separate, already-correct future-timestamp guards found and
  left untouched**: `src.llm.schemas.TradeProposal
  ._validate_market_data_freshness` (`data_timestamp > timestamp` is
  already rejected) and `src.brokers.fidelity._check_market_data_not_stale`
  (`market_data_timestamp > timestamp` is already rejected). Both are
  explicitly protected areas per the task's scope lock (LLM behavior,
  Fidelity integration) and were confirmed to already handle this
  correctly — no change needed or made to either file.

### 3c. Canonical timestamp-selection rule (what was actually implemented)

Not "blindly implement `max(bid_date, ask_date)`" without checking —
confirmed against the investigated semantics and codified as
`_select_quote_timestamp()`:

1. Both `bid_date` and `ask_date` valid → `max(bid_date, ask_date)` —
   the fresher of the two independently-updating quote-side
   timestamps.
2. Only one of `bid_date`/`ask_date` valid → that one.
3. Neither quote-side timestamp valid, but `trade_date` valid →
   `trade_date` — a real, provider-reported timestamp is still
   strictly better than none, demoted to last resort rather than first
   choice (this was the old code's sole behavior; still correct as a
   fallback, just no longer as the primary signal).
4. No provider timestamp at all → `now`, the local capture time of
   this exact HTTP response.

## 4. Required Behavior (Section 3 of the task spec)

1. **UnderlyingQuote** — canonical timestamp reflects the freshest
   actionable bid/ask information; a stale last-trade timestamp cannot
   override it. Verified: `tests/unit/data/test_tradier_provider.py
   ::TestParseQuoteJson::test_stale_trade_date_does_not_override_fresh_bid_ask`.
2. **OptionContract** — identical semantics applied; `bid_timestamp`/
   `ask_timestamp`/`trade_timestamp` kept intact and independently
   verified equal to their raw provider values in the same test that
   verifies the canonical timestamp
   (`TestParseOptionJson::test_G_canonical_timestamp_prefers_fresh_bid_ask_over_stale_trade_date`).
3. **Missing timestamps** — every combination handled deterministically
   per Section 3c above, with no fabricated provider timestamp anywhere.
   The `now` fallback (local capture time) is safe and explicitly
   justified in `_select_quote_timestamp`'s own docstring: it reflects
   the moment *this exact* HTTP response was received, so there is no
   stale timestamp being overridden — there is no provider timestamp at
   all. This exactly mirrors the pre-existing, unmodified fallback
   pattern in `src/data/alpaca_provider.py`
   (`getattr(quote, "timestamp", None) or now`).
4. **Future timestamps / clock anomalies** — `TimestampedModel` now
   fails closed (STALE / raises) for a canonical timestamp more than 1
   minute ahead of `as_of`, independent of `max_age`. Verified at both
   the shared-primitive level (`test_provider.py
   ::TestFutureTimestampDefensiveRule`, 5 tests) and end-to-end through
   a Tradier-sourced quote with a wildly future `bid_date`/`ask_date`
   (`test_tradier_provider.py::test_J_a_wildly_future_bid_date_is_not_treated_as_fresh`).
5. **Freshness threshold** — `DEFAULT_MAX_QUOTE_AGE` (15 minutes) is
   completely untouched; grep-confirmed unchanged, and
   `quality_gate_module_hash`/`risk_module_hash` in Section 6 below
   confirm no downstream threshold changed either.

## 5. Regression Tests (Section 4 of the task spec, items A–K)

All implemented in `tests/unit/data/test_tradier_provider.py` (new
`TestSelectQuoteTimestamp` class plus additions to `TestParseQuoteJson`/
`TestParseOptionJson`) using the exact live epoch-millisecond values
from Section 1 (`trade_date=1790121600002`, `bid_date=1790161815000`,
`ask_date=1790161813000`) plus synthetic realistic values for the
freshness-boundary cases:

| Item | Test | Result |
|---|---|---|
| A | `test_A_trade_date_older_than_bid_ask_is_overridden` | PASS |
| B | `test_B_both_bid_and_ask_present_picks_the_fresher_of_the_two` | PASS |
| C | `test_C_only_bid_date_present` | PASS |
| D | `test_D_only_ask_date_present` | PASS |
| E | `test_E_no_bid_or_ask_falls_back_to_trade_date` (+ option-contract variant) | PASS |
| F | `test_F_no_provider_timestamps_at_all_falls_back_to_now` (+ option-contract variant) | PASS |
| G | `test_G_canonical_timestamp_prefers_fresh_bid_ask_over_stale_trade_date` (option contract; verifies canonical + all 3 per-side fields) | PASS |
| H | `test_H_stale_bid_ask_still_fails_freshness` (quote + option-contract variants) | PASS |
| I | `test_I_fresh_bid_ask_passes_freshness_even_with_a_very_old_last_trade` (quote + option-contract variants) | PASS |
| J | `test_J_a_wildly_future_bid_date_is_not_treated_as_fresh` | PASS |
| K | `test_K_existing_field_mapping_unaffected_by_the_timestamp_fix` + all 9 pre-existing `TestParseOptionJson`/`TestParseQuoteJson` tests | PASS |

Plus the general architecture-level tests in `tests/unit/data/
test_provider.py::TestFutureTimestampDefensiveRule` (5 tests: tiny
skew still fresh, exactly-at-tolerance-boundary still fresh, materially
future is STALE, materially future raises `StaleDataError`, a huge
`max_age` cannot rescue a future timestamp).

## 6. Production Hash Changes — What Changed and Why It's Correct

| Field | V1.4.5 (old) | V1.4.6 (new) | Why |
|---|---|---|---|
| `tradier_provider_module_hash` | `2d98c32703d8a77b…` | `a62f1f9f0276b75f…` | `_select_quote_timestamp` added and wired into both parse functions — exactly this step's intended change. |
| `data_provider_module_hash` | *(field did not exist)* | `13be9bb450f2fb4d…` | Brand-new field, hashing `src/data/provider.py`, which gained the future-timestamp defensive rule. Not previously covered by any freeze check — added now because this step is the first to touch it (same precedent as V1.4.5's `factory_module_hash`). |

Every other hash is confirmed **unchanged**, including the fields most
relevant to this step's blast radius — `quality_gate_module_hash`
(`957a67b156d06480…`, identical: the data-quality gate calls
`freshness_status` but was not itself modified), `risk_module_hash`
(identical: no Risk Engine file touched), `quant_module_hash`
(identical), `paper_broker_module_hash` (identical: PaperBroker's own
`require_fresh` call site was not touched, only the primitive it calls
into) — plus `wheel_module_hash`, `lifecycle_module_hash`,
`alpaca_provider_module_hash`, `rate_limiter_module_hash`,
`portfolio_module_hash`, `review_module_hash`,
`run_validation_cycle_script_hash`, `confirm_candidate_script_hash`,
`factory_module_hash`, `smoke_tradier_script_hash`,
`control_loop_projection_module_hash`, `strategy_library_version`,
every `config_hash:*`, every `prompt_hash:*`, and `claude_md_hash`
(Section 7's `make verify-freeze` output).

## 7. FREEZE

`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.5`/`1.4.5` to `PAPER_TRADING_V1.4.6`/`1.4.6`. One
new manifest field/check was added, following this codebase's existing
pattern (a whole-file hash, the same mechanism `factory_module_hash`
already established, not a new bespoke structural scan — no new
class of check was needed for this step's change, since a
straightforward whole-file hash already fully captures "did
`src/data/provider.py` change").

Exact `make verify-freeze` output against the freeze commit (Section
9), **58 of 58 checks passing**:

```
./scripts/verify_freeze.sh
[OK  ] manifest_exists: loaded 1.4.6
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
[OK  ] data_provider_module_hash: unchanged
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

PAPER_TRADING_V1.4.6 / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR FINAL PRE-VALIDATION ACCEPTANCE
```

`make verify-freeze` legitimately **failed before re-freezing** against
the V1.4.5 manifest — exactly one check, `tradier_provider_module_hash:
DRIFTED since freeze` — confirmed via a dedicated run against the old
manifest before this step's re-freeze, exactly as expected for a step
that intentionally modifies that file, not treated as an anomaly.

## 8. Required Testing (Section 8 of the task spec)

### 8a. Focused Tradier provider timestamp tests

```
pytest -q tests/unit/data/test_tradier_provider.py
70 passed
```

### 8b. Relevant freshness/data-quality tests

```
pytest -q tests/unit/data/ tests/acceptance/test_tradier_market_data_only.py tests/acceptance/test_tradier_live_outer_cycle.py
377 passed, 1 skipped
```

### 8c. Relevant validation-cycle tests

```
pytest -q tests/acceptance/test_run_validation_cycle_cli.py tests/acceptance/test_review_only_daily_cycle.py tests/unit/data/test_quality_gate.py tests/unit/portfolio/test_revaluation.py
54 passed
```

### 8d. Full test suite

```
pytest -q
3462 passed, 6 skipped, 2 warnings
```
(V1.4.5 was 3439 passed, 6 skipped — net new: 23 tests, matching this
step's 17 additions to `test_tradier_provider.py`, 5 to
`test_provider.py`, and 1 to `test_freeze.py`.)

### 8e. `make verify-freeze`

**58 of 58 checks passing** (Section 7).

**No unexplained failures anywhere in this step.**

## 9. Live Read-Only Smoke Validation (Section 5 of the task spec)

No `OPTIONS_AGENT_TRADIER_*` environment variables and no `.env` file
are present in this sandbox — confirmed via direct inspection before
any implementation work began. A live, read-only Tradier smoke test
could therefore **not** be performed here. This report relies entirely
on deterministic unit/acceptance tests (Section 8), reconstructing the
exact live raw values (`trade_date=1790121600002`,
`bid_date=1790161815000`, `ask_date=1790161813000`) the operator's own
earlier live smoke test surfaced, per the task specification's explicit
allowance: "If credentials are unavailable in your environment, say so
explicitly and rely on deterministic tests. The local operator will
perform the final real Tradier smoke test separately."

## 10. Git Commit, Tag, and Manifest Hash

- **Implementation commit** (the timestamp-selection fix, the
  future-timestamp defensive rule, every new/changed test file, and
  the `FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bump plus the
  new `data_provider_module_hash` check in `src/validation/freeze.py`):
  `6b8ce1653c04c6d624dbd01c7ac5756f7baab9f8`.
- `VALIDATION_MANIFEST.json`'s own `git_commit` field records exactly
  this SHA (manifest generated with a fully clean working tree
  immediately after this commit, before `progress.md` or this report
  were written); `repository_state` recorded as `clean`.
- **Manifest hash:** `55006a8a7fb7bded24a9c04fce1b30d25ae9b986a060e8f23141535dea6f1396`.
- **Freeze-report commit** (this freeze report + regenerated
  `VALIDATION_MANIFEST.json` + `progress.md`'s Step 22.7 entry
  together) and **git tag `paper-trading-v1.4.6`** immediately follow
  the implementation commit above in `git log`.
- V1.0 through V1.4.5 tags and their underlying commits were not
  touched by this step.

## 11. Protected Areas — Explicitly Not Changed (Section 6 of the task spec)

Confirmed untouched by this step, grep- and diff-verified: strategy
implementations (`src/strategies/`), strategy scoring/selector, Quant
thresholds (`src/quant/`), Risk limits (`src/risk/`, `config/
risk_limits.yaml`), portfolio sizing, lifecycle triggers
(`src/lifecycle/`), PaperBroker fill/execution rules
(`src/brokers/paper.py`), Fidelity integration
(`src/brokers/fidelity.py`), LLM behavior (`src/llm/`), the validation
cohort and its records, the operational database
(`data/options_agent.db`), the confirmation workflow
(`src/review/confirmation.py`), the order workflow
(`src/orchestration/pipeline.py`, `src/brokers/order_validator.py`),
and broker permissions (`config/brokers.yaml`). Tradier remains
MARKET-DATA-ONLY (`tradier_market_data_only` check, Section 7).
Fidelity remains MANUAL (`fidelity_manual_execution_only` check,
Section 7). PaperBroker remains simulation-only
(`paper_broker_module_hash` unchanged, Section 6). No live-money
execution path exists or was added anywhere in this step
(`live_trading_disabled` check, Section 7).

## 12. VALIDATION

**PAPER_TRADING_V1.4.6: FROZEN. 90_DAY_VALIDATION: IN_PROGRESS**
(cohort `paper-trading-v1.4.3-validation-2026-09-22`, started
2026-09-22, on the operator's own machine — never started, reset, or
touched from this sandbox; its 2026-09-22 and 2026-09-23 records are
unchanged by this step). **LIVE_TRADING: DISABLED. FIDELITY_EXECUTION:
MANUAL_ONLY. TRADIER: MARKET_DATA_ONLY. NEW_POSITION_EXECUTION:
HUMAN_CONFIRMED_REVIEW_ONLY.**

- No new cohort was created anywhere in this step.
- No existing cohort database was overwritten, reset, or altered.
- No official, state-mutating validation cycle was executed anywhere
  in this session. No `confirm_candidate` call was executed against
  the official cohort anywhere in this session.
- No order/trading endpoint was called anywhere in this session (no
  live Tradier calls were possible at all — no credentials configured
  in this sandbox; the fix itself adds no new HTTP call of any kind,
  only changes which already-parsed field becomes the canonical
  timestamp).
- `data/options_agent.db` does not exist in this sandbox at the time of
  this report.
- No Risk/Lifecycle/Quant bypass exists or was introduced anywhere in
  this step (`risk_module_hash`/`lifecycle_module_hash`/
  `quant_module_hash` confirmed unchanged in Section 6).
- No security assertion was weakened anywhere in this step — one new
  structural check (`data_provider_module_hash`) was added to
  `make verify-freeze`, none removed or loosened, and the future-
  timestamp fix strictly tightens (never loosens) freshness
  enforcement.

**Remaining concern before the operator runs the official validation
cycle:** none identified beyond the standard, already-documented
requirement that the operator perform their own live, real-credential
Tradier smoke test (Section 9) to confirm the fix behaves identically
against a live response as it does against the reconstructed test
fixtures here — this sandbox had no way to do that itself.
