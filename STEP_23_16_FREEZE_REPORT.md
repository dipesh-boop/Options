# STEP 23.16 FREEZE REPORT — PAPER_TRADING_V1.5.16

## Status

**PAPER_TRADING_V1.5.16 / SOFTWARE FREEZE VERIFIED**

A tightly-scoped, sandbox-only corrective release. It fixes exactly
two findings from the read-only architecture audit of the Expanded-
Universe Sandbox's first real cycle (`sandbox-validation-2026-10-09`)
and changes nothing else: no trading-experiment behavior, candidate
economics, Quant logic, Risk logic, portfolio sizing, strategy
eligibility, market-data rules, or execution behavior. The October 9
sandbox cycle and its persisted records are untouched. The official
cohort (`paper-trading-v1.4.3-validation-2026-09-22`) and
`config/universe.yaml` (SPY, QQQ) are unaffected — confirmed by this
same freeze manifest showing zero module-hash drift anywhere outside
version metadata.

## A. Finding 1 — sandbox `proposal_id` overflow risk (FIXED)

**Root cause.** `scripts/run_sandbox_cycle.py` supplied
`generate_candidates` the prefix `"sandbox-validation-scan"` (24
characters) — considerably longer than the official runner's own,
already-corrected `"validation-scan"` (16 characters, fixed in
PAPER_TRADING_V1.5.12 for exactly this failure class). `_next_id`
(`src.workflows.candidate_generation`) builds
`f"{prefix}-{ticker}-{date}-{strategy_tag}-{expiration}-{strike_part}-{counter}"`,
bounded by `TradeProposal.proposal_id: Field(max_length=64)`
(`src/llm/schemas.py`, unmodified, untouched by this release). A
two-leg PUT_CREDIT_SPREAD's longer `strike_part` (two strikes) made it
the most overflow-prone strategy — exactly the category the Oct 9
cycle's `generation_exceptions` counter recorded.

**Fix.** `scripts/run_sandbox_cycle.py` line 440:
`proposal_id_prefix="sandbox-validation-scan"` →
`proposal_id_prefix="sbx-scan"` (8 characters). One line changed.
Verified via repository-wide grep that no other file referenced the
old string. Nothing else in `_next_id`, `TradeProposal.proposal_id`'s
schema constraint, the official `"validation-scan"` prefix, or any
cycle/cohort/account/manifest/experiment-version/candidate ID was
touched. No post-generation truncation was added (not architecturally
required).

**Tests** (`tests/unit/workflows/test_candidate_generation.py`,
`TestSandboxProposalIdLengthRegressionV1516`, 11 tests): a PCS for
GOOGL (the longest ticker in the real 12-symbol sandbox universe) with
decimal, non-round strikes under the `"sbx-scan"` prefix constructs
without raising and fits `max_length=64`; a worst-case counter
("99999") still fits (61 ≤ 64, computed exactly, not just "ample
headroom"); the resulting object is a genuinely valid, fully
constructed `TradeProposal`; ids remain deterministic (same inputs →
same id) and non-colliding across both different scan dates and
multiple candidates in one scan; the official `"validation-scan"`
prefix is confirmed unchanged by direct AST inspection of
`scripts/run_validation_cycle.py`; single-leg CASH_SECURED_PUT
generation is unaffected; every economics/leg/timestamp field is
identical between a sandbox-prefixed and an official-prefixed call
against the same chain; and `TradeProposal`'s own `max_length=64`
constraint is proven NOT weakened (a 65-character id is still rejected
by the schema directly, independent of `generate_candidates`).

## B. Finding 2 — misleading "ranked candidate audit" console label (FIXED)

**Root cause.** `scripts/run_sandbox_cycle.py
._print_ranked_candidate_audit` printed `len(scan.scanned)` — every
Quant-evaluated candidate, Risk-rejected ones included — as "candidates
ranked this cycle". That collided with the persisted funnel's own,
materially smaller `candidates_ranked` field
(`src.workflows.candidate_funnel.build_candidate_funnel`, computed
only over the Risk-approved-with-a-valid-score subset) — exactly the
Oct 9 cycle's observed discrepancy (21 printed vs. 10 persisted).

**Fix.** Console wording only. The heading now reads "candidate
evaluation audit -- candidates scanned/evaluated this cycle"; the
empty-scan case reads "no candidate was scanned/evaluated this cycle".
The function's own docstring was updated to document the distinction
explicitly. Every per-candidate diagnostic line (ticker, strategy,
expiration, legs, `max_profit`/`max_loss`/`capital_required`/
`risk_adjusted_return`, `risk_decision`/`risk_reason`, the `(SELECTED)`
marker) is unchanged. `scan.scanned`, survivor/Risk filtering, ranking,
`scan.best`, candidate persistence, and human-review behavior were not
touched — `src.portfolio.opportunity_scan.scan_and_rank_opportunities`
and `src.workflows.candidate_funnel.build_candidate_funnel` were not
modified at all.

**Tests** (`tests/unit/workflows/test_sandbox_console_audit_v1516.py`,
8 tests, against a real mixed scan built from one Risk-APPROVED and
one Risk-REJECTED candidate under one portfolio — never hand-set
`risk_decision` fields): (1) a Risk-rejected candidate's diagnostic
line is printed in the audit; (2) `candidates_ranked` excludes it
(1, not 2, even though `len(scan.scanned) == 2`); (3) `scan.best` is
never the Risk-rejected candidate; (4) `ReviewedCandidate` is proven,
by direct source inspection, to be constructed only from `scan.best`
inside the `scan.best is not None` branch — never from any other
`scan.scanned` entry — so a Risk-rejected candidate can never be
persisted for human review; (5) the console heading never says
"ranked candidate audit" or "candidates ranked this cycle" (for both
the real mixed scan and the empty-scan case), and does say "candidate
evaluation audit" / "scanned/evaluated this cycle"; (6) the six named
funnel fields (`candidates_generated`, `quant_passed`, `risk_passed`,
`candidates_ranked`, `candidates_selected`,
`candidates_persisted_for_review`) carry exactly their pre-existing,
documented meaning, unchanged.

## C. Cohort status finding — NOT fixed, documented only

The sandbox cohort's `CohortRecord.status` remaining `"created"` after
its first successful cycle is, per the prior read-only audit,
write-once-at-initialization-only behavior shared identically with the
official cohort's own `CohortRecord` — `status` is read only for
display (PDF/XLSX export, `sandbox_status.py`), never branched on for
control flow, and no automated process anywhere in this codebase ever
transitions `created` → `active`. This release makes **no** change
here: no `CohortRecord` semantic change, no transition logic added, no
official cohort behavior touched. Recorded as a known, LOW-severity
reporting/metadata item for possible future work, exactly as the prior
audit recommended.

## D. Experiment-version identity — unaffected, by architecture

`src.validation.experiment_version.compute_experiment_version_id`
hashes only three config files' contents, a version-label string
(`SANDBOX_SOFTWARE_FREEZE_VERSION`), a market-data-provider descriptor,
and a `risk_data_wiring` boolean — never source code. Since this
release touches no config file and no sandbox identity constant,
`SANDBOX_SOFTWARE_FREEZE_VERSION` in `src/portfolio/sandbox_identity.py`
is deliberately left at `"PAPER_TRADING_V1.5.15"` — the architecture
does not require a new hash for a code-only fix, and bumping it would
have been an unrequested, out-of-scope identity change. No new sandbox
cohort was created; the existing cohort's start date, NAV, and
experiment-version identity are all unchanged.

## E. Files changed

- `scripts/run_sandbox_cycle.py` — Fix 1 (one line) + Fix 2 (console
  wording + docstring in `_print_ranked_candidate_audit`). Sandbox-only
  file, not part of the freeze manifest's hashed module set (same as
  PAPER_TRADING_V1.5.15's own entry for this file).
- `tests/unit/workflows/test_candidate_generation.py` — 11 new tests
  (`TestSandboxProposalIdLengthRegressionV1516`).
- `tests/unit/workflows/test_sandbox_console_audit_v1516.py` — new
  file, 8 tests.
- `tests/unit/validation/test_freeze.py` — updated the two hardcoded
  `freeze_name`/`freeze_version` assertions to `"PAPER_TRADING_V1.5.16"`
  / `"1.5.16"`, per the existing per-release convention.
- `src/validation/freeze.py` — `FREEZE_NAME`/`MANIFEST_VERSION` bumped
  to `PAPER_TRADING_V1.5.16`/`1.5.16`, plus a new dated comment block
  documenting this release's exact scope.
- `VALIDATION_MANIFEST.json` — regenerated via `make freeze-manifest`.
  Diff confined to `manifest_version`, `freeze_name`,
  `freeze_timestamp`, `git_commit`, `repository_state`,
  `freeze_version`, and `manifest_hash` — every one of the ~45 hashed
  module/config/prompt/safety fields is byte-identical to
  PAPER_TRADING_V1.5.15's manifest.
- `STEP_23_16_FREEZE_REPORT.md` — this file.
- `progress.md` — new entry for this release.

No operational database, no secret, no official-path file
(`scripts/run_validation_cycle.py`, `config/universe.yaml`,
`config/risk_limits.yaml`, `config/validation.yaml`, `config/brokers.yaml`,
`src/risk/*`, `src/llm/*`, `src/brokers/fidelity.py`,
`src/portfolio/opportunity_scan.py`, `src/workflows/candidate_funnel.py`,
`src/validation/records.py`, `src/validation/cohort.py`) was modified.

## F. Test baseline

Pre-change baseline (unmodified PAPER_TRADING_V1.5.15 working tree,
confirmed by `git stash` + re-run): **3970 passed, 9 failed, 6 skipped**
— the same 9 known, date-dependent pre-existing failures already
documented in `STEP_23_15_FREEZE_REPORT.md` §I, identical node IDs:

- `tests/acceptance/test_market_hours_gate.py::TestBackendPostInsideSession::test_reaches_the_mocked_runner_path`
- `tests/acceptance/test_market_hours_gate.py::TestCliInsideSession::test_reaches_the_mocked_normal_path`
- `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate`
- `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_control_cycle_record_shows_zero_silent_generation_exceptions`
- `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`
- `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`
- `tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses::test_tradier_production_reaches_the_mutating_cycle`
- `tests/unit/dashboard/test_frontend_control_center.py::TestValidationEndpointIdempotencyUnaffected::test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op`
- `tests/unit/dashboard/test_operator_status.py::TestValidationCycleRunRoute::test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome`

After this release's changes: **3989 passed** (3970 + 19 new: 11 Fix-1
+ 8 Fix-2 tests), **9 failed** (identical node IDs, identical root
cause), **6 skipped** — zero new regressions.

`make verify-freeze`: **PASS**, all ~45 checks `[OK]`, final line
`PAPER_TRADING_V1.5.16 / SOFTWARE FREEZE VERIFIED`.

## G. Safety confirmations

- Official validation path (`scripts/run_validation_cycle.py`): zero
  behavioral change; `proposal_id_prefix="validation-scan"` confirmed
  unchanged (both by direct read and by a dedicated regression test).
- `config/universe.yaml` (SPY, QQQ) and every official risk/Quant/
  liquidity/freshness/DTE/sizing/hurdle/ranking rule: unchanged —
  confirmed by the freeze manifest's zero module-hash drift.
- Real sandbox universe (`config/universe_sandbox.yaml`): unchanged,
  still exactly the 12 named symbols (SPY, QQQ, IWM, DIA, AAPL, MSFT,
  NVDA, AMZN, META, GOOGL, JPM, XOM) — not touched this release.
- No operational database (`data/options_agent.db`,
  `data/options_agent_sandbox.db`) was read, modified, initialized, or
  reset. No test depends on either file; all persistence tests use
  isolated temporary databases/fakes.
- The October 9 sandbox cycle was neither rerun nor altered.
- No real network call was made to Tradier, Fidelity, or IBKR, in
  development or in tests — fake/offline providers only.
- No candidate was confirmed and no PaperBroker position was created
  during this work.
- The sandbox cohort was not reinitialized, reset, or replaced; no new
  cohort was created.
- No `.env` secret was printed, inspected, or modified.

## H. CLAUDE.md invariant checklist

1. Risk Engine sole authority — unchanged; `evaluate_trade_proposal`
   untouched.
2. No LLM computes an authoritative number — unaffected; no LLM file
   touched.
3. Fidelity manual-only — unaffected; `src/brokers/fidelity.py` not
   touched.
4. Broker capability set never inferred — unaffected;
   `config/brokers.yaml` not touched.
5. Every figure computed once, cross-checked — unaffected; `src/risk/`
   not touched.
6. Capital preservation over return — unaffected; no limit/threshold
   changed.
7. No live trading — unaffected; `BrokerEnvironment` not touched.

**PAPER_TRADING_V1.5.16: FROZEN. PAPER_TRADING_V1.5.15 OFFICIAL COHORT:
UNCHANGED, ACTIVE. SANDBOX COHORT (sandbox-validation-2026-10-09):
UNCHANGED, HISTORICAL. LIVE_TRADING: DISABLED. FIDELITY_EXECUTION:
MANUAL_ONLY.**
