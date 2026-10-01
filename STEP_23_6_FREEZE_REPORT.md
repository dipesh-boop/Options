# STEP 23.6 FREEZE REPORT — PAPER_TRADING_V1.5.6

## Status

**PAPER_TRADING_V1.5.6 / SOFTWARE FREEZE VERIFIED**

A narrow hotfix. Cohort `paper-trading-v1.4.3-validation-2026-09-22`
remains ACTIVE, untouched, and unreset by this step. The 2026-10-01
`ControlCycleRecord` (the evidence of the pre-hotfix defect) was not
rewritten, regenerated, or backfilled.

## Executive Summary

V1.5.5's candidate-funnel observability (frozen the previous step) did
exactly what it was built to do: the first real production cycle
(2026-10-01) surfaced a concrete integration defect rather than a vague
"zero candidates." Evidence: provider healthy, 2 symbols succeeded, 3768
contracts seen, but `expirations_eligible=0`, `expirations_rejected=12`,
`candidates_generated=0`.

**Root cause, confirmed from source**:
`TradierMarketDataProvider.get_option_chain()` (`src/data/tradier_provider.py`)
fetches the nearest `max_expirations` (6) expirations by calendar date,
with **no DTE awareness at all**. On 2026-10-01 those nearest 6
expirations were 0/1/4/5/6/7 DTE — every one of them fails the
candidate engine's own `min_dte=20, max_dte=45` window
(`src.workflows.candidate_generation.QuantFilterConfig`). Real eligible
expirations existed in the market that day (Oct 23 = 22 DTE, Oct 30 = 29
DTE, Nov 6 = 36 DTE, Nov 13 = 43 DTE) — their chains were simply never
requested. This is a retrieval/integration defect, not evidence that
DTE/Quant/Risk/liquidity policy is too restrictive, and not evidence
that more symbols or strategies are needed.

**Fix**: a new, optional, provider-neutral capability interface,
`DteWindowOptionChainProvider` (`src/data/provider.py`), mirroring the
exact pattern V1.5.4 already established for `HistoricalDataProvider`.
Only `TradierMarketDataProvider` implements it — Alpaca's
`get_option_chain()` already returns every expiration in a single call
and has no nearest-N retrieval problem to fix. The new method,
`get_option_chain_for_dte_window(symbol, *, min_dte, max_dte, as_of,
diagnostics=None)`, selects the provider's own expiration calendar,
filters to the exact `min_dte <= (expiration - as_of).days <= max_dte`
window `_eligible_expirations` already uses, and fetches full chains
only for the (at most `max_expirations`, nearest-to-`min_dte`-first)
selected expirations. `min_dte`/`max_dte` are always caller-supplied —
`QuantFilterConfig` remains the sole DTE-policy owner, never duplicated
inside the provider.

`scripts/run_validation_cycle.py`'s fetch loop now branches per ticker:
universe-only tickers get the new DTE-windowed fetch; position-only
tickers keep the unmodified `get_option_chain()` (lifecycle monitoring
still needs near-term expirations); a ticker that is both gets **both**
fetches, merged via the new `src.data.option_chain.merge_option_chains`
— never one at the expense of the other.

## Architecture Trace (confirmed from source before implementation)

1. `MarketDataProvider` (`src/data/provider.py`) — abstract interface;
   `get_option_chain(symbol)` has no DTE parameter anywhere.
2. `TradierMarketDataProvider.get_expirations()` — already returns the
   provider's full expiration calendar (`list[date]`), unfiltered.
3. `TradierMarketDataProvider.get_option_chain()` — the defect: fetches
   `expirations[: self._config.max_expirations]` (nearest-N by calendar
   date), no DTE filter.
4. `TradierMarketDataProvider.get_option_chain_for_expiration()` —
   already fetches one expiration's full chain; reused unmodified by
   the new method.
5. Callers of `get_option_chain()`: `scripts/run_validation_cycle.py`'s
   fetch loop (the only production caller found) and
   `src/review/confirmation.py`'s fresh-quote refetch at confirm time
   (see "Remaining architecture issues" below — deliberately NOT
   touched this step).
6. Existing-position lifecycle monitoring (`run_outer_cycle` →
   `run_control_cycle`) consumes whatever chain the fetch loop supplies
   it — it has no DTE opinion of its own and must keep seeing near-term
   expirations (a position can legitimately be under 20 DTE).
7. `src.portfolio.opportunity_scan.scan_and_rank_opportunities` →
   `src.workflows.candidate_generation.generate_candidates` →
   `_eligible_expirations(chain, as_of, quant_filter)` — the existing,
   UNCHANGED DTE-eligibility filter: `min_dte <= (expiration -
   as_of.date()).days <= max_dte`. This is the exact semantics the new
   provider method was built to match.
8. `QuantFilterConfig` (`src.workflows.candidate_generation`) — the
   sole owner of `min_dte`/`max_dte` (defaults 20/45). Confirmed no
   other module defines a competing DTE threshold.
9. Alpaca equivalent (`src/data/alpaca_provider.py`) — `get_option_chain`
   has no per-expiration request loop; it returns every expiration's
   contracts from one call. No retrieval-side fix needed or made.
10. Existing provider-interface compatibility requirement: every
    pre-existing `MarketDataProvider` implementation (the acceptance
    suite's `FakeMarketDataProvider`, Alpaca) must be completely
    unaffected. Addressed by making the new capability optional and
    interface-gated (`isinstance(provider, DteWindowOptionChainProvider)`),
    never a change to the base `MarketDataProvider` contract.
11. Caching/deduplication: none existed before this step and none was
    added — each call still issues its own requests, as before.
12. Rate-limit priority: unchanged. The new method defaults to the same
    `RateLimitPriority.P4_OPPORTUNITY_SCANNING` as `get_option_chain`,
    and accepts a caller override identically.

## Why strategy policy was not duplicated in the provider

`DteWindowOptionChainProvider.get_option_chain_for_dte_window` takes
`min_dte`/`max_dte` as **required, no-default** keyword arguments (proven
by `tests/unit/data/test_tradier_provider.py::TestGetOptionChainForDteWindow
::test_min_dte_and_max_dte_have_no_default_the_caller_must_always_supply_them`,
plus a dedicated AST-based test that no bare `20`/`45` integer literal
appears anywhere in the method's executable body). `scripts/run_validation_cycle.py`
hoists exactly one `QuantFilterConfig()` instance and passes its
`min_dte`/`max_dte` into both the fetch and the later
`OpportunityScanConfig` — one source of truth, never two.

## Expiration-selection algorithm (exact)

```python
eligible = sorted(e for e in expirations if min_dte <= (e - as_of).days <= max_dte)
selected = eligible[: self._config.max_expirations]
```

- Same calendar-day DTE semantics as `_eligible_expirations` — proven
  identical by a dedicated cross-check test
  (`test_date_semantics_match_candidate_generation_eligible_expirations`)
  that runs both functions against the same synthetic chain and asserts
  equal output sets.
- Zero eligible expirations → an empty-contracts `OptionChain` is
  returned, never a substituted out-of-window expiration (fail honest,
  never fail silent-wrong).
- More eligible expirations than the bound → deterministic,
  closest-to-`min_dte`-first selection (ascending date order — no
  randomness, no LLM, no trade-count optimization).
- Request bound: reuses the **existing** `TradierConfig.max_expirations`
  field (default 6) — no new config value was introduced.

## Request-count comparison (before vs after)

Both `get_option_chain` and `get_option_chain_for_dte_window` make the
same per-expiration shape of calls (1 outer underlying quote + 1
expirations list, then `get_option_chain_for_expiration`'s own
quote+chain pair per selected expiration). Exact numbers, reproducing
the 2026-10-01 16-expiration production calendar with
`max_expirations=6`
(`tests/unit/data/test_tradier_provider.py::test_request_count_is_the_same_shape_as_get_option_chain_not_more`):

| Path | Expirations selected | Total HTTP calls |
|---|---|---|
| `get_option_chain` (pre-hotfix) | 6 (nearest by date — all 0-7 DTE, all out-of-window) | 14 (2 + 2×6) |
| `get_option_chain_for_dte_window` (post-hotfix) | 4 (the only 4 that fall in `[20,45]` DTE) | 10 (2 + 2×4) |

The DTE-aware fetch never issues **more** requests than the pre-hotfix
path for the same `max_expirations` bound — confirmed separately that
when 10+ expirations are eligible, the fetch is still capped at
`max_expirations` chain requests, never one per eligible expiration
(`test_request_bound_never_exceeded_regardless_of_how_many_expirations_are_eligible`).
This is not "20/30/50/all" — the bound was never raised.

## Lifecycle vs. opportunity-scan separation

`scripts/run_validation_cycle.py`'s fetch loop now computes
`position_tickers` and `universe_tickers` separately:

- Universe-only ticker + provider implements the capability → DTE-windowed
  fetch only.
- Position-only ticker (or any provider that doesn't implement the
  capability, e.g. Alpaca) → unchanged `get_option_chain()`.
- Ticker in both sets → **both** fetches, merged via
  `merge_option_chains(lifecycle_chain, scan_chain)` (dedup by
  `(expiration, strike, right)`, earlier-of-the-two timestamps,
  underlying/source retained from the lifecycle chain).

Proven at the acceptance level, not just unit level
(`tests/acceptance/test_dte_window_chain_retrieval.py`), against the
real `scripts/run_validation_cycle.py` entry point with a fake provider
that records every call it receives:
- universe-only ticker → `get_option_chain_for_dte_window` called
  exactly once, `get_option_chain` never called.
- ticker that is both a position and in the universe → both methods
  called exactly once each, and the cycle completes successfully
  (existing-position Lifecycle Engine evaluation still runs against the
  merged chain).
- `tests/acceptance/test_review_only_daily_cycle.py` (pre-existing,
  unmodified) continues to pass unchanged against its own
  `FakeMarketDataProvider`, which does NOT implement
  `DteWindowOptionChainProvider` — proving a provider that only
  implements the base `MarketDataProvider` contract is completely
  unaffected by this hotfix.

## Funnel diagnostics changes

Two additive layers, zero new trading-affecting state:

1. **Provider-selection diagnostics** (`src.data.provider
   .DteWindowSelectionDiagnostics`): `provider_expirations_returned`,
   `expirations_in_window`, `expirations_selected`,
   `expirations_skipped_outside_window`, `expirations_skipped_due_to_bound`
   — five plain integer counts, populated strictly after the real
   selection, printed per-ticker by `run_validation_cycle.py`, never
   persisted into `CandidateFunnel`/`ControlCycleRecord`. Structurally
   proven incapable of carrying a secret/token (every field is `int`;
   a dedicated test asserts `vars(diag)` contains only `int` values,
   and another runs a real call with the test token present and asserts
   the token string never appears in the diagnostics object).

2. **Candidate-generation funnel fix** (`src.workflows.candidate_funnel
   .build_candidate_funnel`): closes the exact V1.5.5 observability gap
   the incident exposed. Before this step, `CandidateFunnel.expirations_rejected`
   was visible but **no entry ever appeared in `rejection_reasons`/
   `top_bottlenecks`** for it — so on 2026-10-01 an unrelated,
   coincidental `COVERED_CALL_NO_SHARES` rejection could misleadingly
   look like the dominant bottleneck when expiration eligibility had
   actually eliminated every candidate first. Fix: `rejected_expirations
   = diag.expirations_seen - diag.expirations_eligible`; if positive,
   `reason_counter[("expiration", "EXPIRATION_DTE_OUT_OF_RANGE")] +=
   rejected_expirations`. Zero new `FunnelDiagnostics` fields — both
   `expirations_seen` and `expirations_eligible` were already recorded
   by V1.5.5. These are candidate-generation chain diagnostics (built
   from whatever chain was actually supplied to `generate_candidates`),
   distinct from the provider-selection diagnostics above — after this
   hotfix, a provider expiration outside the requested window never has
   its full chain fetched at all, so it can never appear in this count;
   this is intentional and does not change what the count measures
   (expirations seen in the chain that was supplied, vs. eligible under
   policy).

## Files changed (implementation)

- `src/data/provider.py` — added `DteWindowSelectionDiagnostics` and
  `DteWindowOptionChainProvider` (new, additive).
- `src/data/tradier_provider.py` — added
  `get_option_chain_for_dte_window`; class now also inherits
  `DteWindowOptionChainProvider`. `get_option_chain` itself: **unchanged**.
- `src/data/option_chain.py` — added `merge_option_chains` (new,
  additive).
- `scripts/run_validation_cycle.py` — fetch loop rewritten to branch
  per ticker as described above; `QuantFilterConfig()` hoisted above
  the fetch loop and reused for `OpportunityScanConfig` (previously
  constructed inline, same values, just not shared).
- `src/workflows/candidate_funnel.py` — added the
  `EXPIRATION_DTE_OUT_OF_RANGE` rejection-reason derivation described
  above.
- `src/validation/freeze.py` — (a) fixed a freeze-check regression:
  `_verify_historical_data_capability_installed` used an exact-string
  match for `TradierMarketDataProvider`'s two-base-class declaration,
  which broke the instant a third base class
  (`DteWindowOptionChainProvider`) was added; replaced with a
  regex-based check extracting the base-class list and confirming both
  `MarketDataProvider` and `HistoricalDataProvider` are present as
  substrings — future-proof against further base classes; (b) version
  bump (see Freeze section below).
- `tests/acceptance/test_tradier_market_data_only.py` — added
  `get_option_chain_for_dte_window` to the public-surface allowlist
  (a structural test enumerating every permitted public method beyond
  the base `MarketDataProvider` contract).

## Files intentionally NOT changed

- `src/workflows/candidate_generation.py` — `QuantFilterConfig`,
  `_eligible_expirations`, `generate_candidates` itself: byte-for-byte
  unchanged. This hotfix changes what chain the candidate engine
  *receives*, never its own eligibility logic.
- `src/risk/`, `src/quant/`, `src/strategies/`, `src/lifecycle/` — not
  touched.
- `config/universe.yaml`, `config/risk_limits.yaml`,
  `config/operations.yaml`, `config/brokers.yaml` — not touched. No
  threshold, universe entry, or strategy activation changed.
- `src/review/confirmation.py` / `scripts/confirm_candidate.py` — not
  touched (see "Remaining architecture issues").
- `src/brokers/paper.py`, `src/brokers/fidelity.py` — not touched.
- `src/portfolio/risk_data.py` — not touched; `risk_data_wiring.enabled`
  remains `false` for the active cohort, and `apply_risk_data_wiring`'s
  fail-closed/date-intersection behavior (V1.5.4) is unmodified.
- `src/data/alpaca_provider.py` — not touched; confirmed it has no
  nearest-N retrieval defect to fix (no per-expiration request loop).

## Test results

- `tests/unit/data/test_tradier_provider.py`: **123 passed** (101
  pre-existing + 22 new, covering the `TestGetOptionChainForDteWindow`
  class — expiration selection, the exact 2026-10-01 calendar
  reproduction, boundary inclusion/exclusion at min/max DTE, bounded
  deterministic selection, date-semantics parity with
  `candidate_generation.py`, no hard-coded 20/45 via AST inspection, no
  default `min_dte`/`max_dte`, diagnostics accuracy, request-count
  comparison, rate-limit priority propagation, no secret/token in
  diagnostics, `isinstance` capability check).
- `tests/unit/data/test_option_chain.py`: **29 passed** (23 pre-existing
  + 6 new `TestMergeOptionChains` tests).
- `tests/unit/workflows/test_candidate_funnel.py`: **18 passed** (13
  pre-existing + 5 new `TestExpirationDteOutOfRangeRejectionReason`
  tests, including a direct reproduction of the 2026-10-01 incident
  shape against the real pipeline).
- `tests/acceptance/test_dte_window_chain_retrieval.py` (new): **3
  passed** — proves the real `scripts/run_validation_cycle.py` fetch
  loop takes the DTE-windowed branch for universe tickers and the
  merge branch for a ticker that is both a position and in the
  universe.
- `tests/acceptance/test_tradier_market_data_only.py`: **19 passed**
  (allowlist regression fixed).
- `tests/unit/validation/test_freeze.py`: **94 passed** (freeze-check
  regex fix verified; version-string assertions bumped to V1.5.6).
- `tests/acceptance/test_review_only_daily_cycle.py` and the rest of
  `tests/acceptance/`: **341 passed, 2 skipped** — unmodified,
  confirming backward compatibility for providers that don't implement
  the new capability.
- **Full suite**: `python -m pytest -q` → **3740 passed, 6 skipped**,
  zero failures.
- `./scripts/verify_freeze.sh` → **PAPER_TRADING_V1.5.6 / SOFTWARE
  FREEZE VERIFIED** (every named invariant check — Fidelity manual-only,
  live trading disabled, Tradier market-data-only, PaperBroker cannot
  be bypassed, opportunity scan never outranks risk monitoring, daily
  cycle never calls `place_order`, dashboard cannot confirm candidates,
  market-hours gate precedes mutation, risk-data-wiring fail-closed and
  inactive for the active cohort, candidate-funnel observability-only,
  historical-data capability installed, correlation date-intersection
  intact — all reported `[OK]`).

## Invariants confirmed unchanged (exact proof)

| Invariant | Proof |
|---|---|
| `min_dte=20`/`max_dte=45` | `src/workflows/candidate_generation.py` `QuantFilterConfig` defaults: byte-for-byte unchanged (no diff). |
| Universe (SPY, QQQ) | `config/universe.yaml`: no diff. |
| Strategy activation | `config/brokers.yaml` `allowed_strategies`: no diff. |
| Quant Engine | `src/quant/`: no diff; full quant test suite unchanged and passing. |
| Risk Engine | `src/risk/`: no diff; full risk test suite (including `test_architecture_boundary.py`) unchanged and passing. |
| Liquidity/freshness thresholds | `src/risk/trade_risk.py`, `src/data/provider.py`'s `DEFAULT_MAX_QUOTE_AGE`: no diff. |
| Lifecycle policies | `src/lifecycle/`: no diff; full lifecycle suite unchanged and passing. |
| `risk_data_wiring.enabled=false` | `config/operations.yaml`: no diff; `verify_freeze.sh`'s `risk_data_wiring_inactive_for_active_cohort` check: `[OK]`. |
| Tradier market-data-only | `verify_freeze.sh`'s `tradier_market_data_only` check: `[OK]` (no order/trading-shaped identifier anywhere in `src/`); new public method `get_option_chain_for_dte_window` is itself a read-only chain-fetch method, added to the explicit public-surface allowlist test. |
| Fidelity manual-only | `src/brokers/fidelity.py`: not touched; `verify_freeze.sh`'s `fidelity_manual_execution_only` check: `[OK]`. |
| PaperBroker execution | `src/brokers/paper.py`: not touched; `daily_cycle_never_calls_place_order` check: `[OK]`. |
| Candidate confirmation | `src/review/confirmation.py`, `scripts/confirm_candidate.py`: not touched; `dashboard_cannot_confirm_candidates` check: `[OK]`. |
| Correlation date-intersection (V1.5.4) | `src/portfolio/risk_data.py`: not touched; `correlation_alignment_uses_date_intersection` check: `[OK]`. |
| Historical `get_bars` (V1.5.4) | `TradierMarketDataProvider.get_bars`: not touched; `historical_data_capability_installed` check: `[OK]` (regex-fixed, still passing). |
| Active cohort identity/history | `paper-trading-v1.4.3-validation-2026-09-22`: no code in this diff reads/writes a cohort id or calls `start_new_cohort`/`reset`; `validation_cohort_not_started` freeze check confirms freeze itself never starts one. |

## No official validation cycle / no operational DB access

Every new and modified test in this step runs against `tmp_path`
sqlite files, `InMemory*` stores, or pure synthetic data — none
references `data/options_agent.db`, and the repository has no tracked
`data/` directory at all. No test in this step placed a PaperBroker
order against the real account, confirmed a candidate, or called any
real brokerage/order API (no network call is possible — every HTTP
client in every new/modified test is a `FakeHttpClient`/fake provider).
No secret or token was persisted in any diagnostics object (dedicated
tests above).

## Remaining architecture issues (not fixed this step, by design)

1. **`src/review/confirmation.py`'s fresh-quote refetch is still
   nearest-N, unfixed.** Once this hotfix lets a legitimately 20-45 DTE
   candidate through the daily scan, `confirm_candidate.py`'s own
   `get_option_chain(ticker)` refetch at confirmation time could, in
   principle, fail to find that exact expiration again if the nearest-N
   calendar window has since shifted. This is the same root cause as
   the hotfix just applied, in a different call site. Deliberately NOT
   fixed here: the task was scoped as a narrow retrieval fix for
   opportunity scanning, with an explicit instruction not to mix
   unrelated architecture changes into V1.5.6. Flagged here for a
   future, separately-tracked step, exactly as the market-hours/
   lifecycle gate issue below has been handled across V1.5.1-V1.5.5.
2. **Market-hours/lifecycle gate** (first identified in V1.5.1, reaffirmed
   in V1.5.5): the top-level market-hours gate in
   `run_validation_cycle.py` can suppress existing-position lifecycle/
   risk monitoring when the new-position scan window is closed. This
   hotfix's own lifecycle-vs-scan separation does not interact with
   that gate (both branches of the fetch loop still only run after the
   gate passes) — the DTE retrieval change does not make lifecycle
   behavior any less safe than before, so per the task's own
   instruction this is left unchanged and re-flagged as a separately
   tracked safety-hardening item, not addressed in V1.5.6.

## Freeze

- `FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version`: `PAPER_TRADING_V1.5.6` /
  `1.5.6` / `"1.5.6"` (bumped in place from V1.5.5; prior freeze
  reports/manifests remain recoverable via git history / tags, per the
  established convention).
- `VALIDATION_MANIFEST.json`: regenerated via `make freeze-manifest`
  (`python -m src.validation.freeze build`).
- `./scripts/verify_freeze.sh`: **PAPER_TRADING_V1.5.6 / SOFTWARE
  FREEZE VERIFIED**, every check `[OK]`.
- Database schema: unchanged (`database_schema_version: 1.0.0 ==
  current 1.0.0`) — no migration was required or performed.
- No blind nearest-N-only opportunity retrieval remains: the only
  production caller of market-data-chain fetching for the opportunity
  scan (`scripts/run_validation_cycle.py`) now uses the DTE-windowed
  path whenever the configured provider supports it.

## Git

- Implementation commit: see repository log (source + test changes
  described above).
- Freeze-artifacts commit: this report, `VALIDATION_MANIFEST.json`,
  `progress.md`, and the `src/validation/freeze.py` /
  `tests/unit/validation/test_freeze.py` version bumps.
- Tag: `paper-trading-v1.5.6` (tag push, if attempted, has historically
  hit an HTTP 403 in this environment every prior freeze — reported,
  never force-pushed).

## Recommended next step — DO NOT IMPLEMENT IT

Fix `src/review/confirmation.py`'s fresh-quote refetch to use the same
`DteWindowOptionChainProvider` capability this hotfix introduced, so a
candidate's exact expiration remains fetchable at confirmation time
even after the nearest-N calendar window has shifted since the scan
that found it. This is closely related to V1.5.6 but was deliberately
scoped out as a confirmation-time, not scan-time, concern — it should
be its own narrowly-scoped step with its own read-only architecture
trace, not bundled into this one.
