# STEP_22_1_FREEZE_REPORT.md

## Pre-Validation Controlled Amendment — Alpaca OPRA Market Data

**PAPER_TRADING_V1.1: FROZEN**
**90_DAY_VALIDATION: NOT_STARTED**
**LIVE_TRADING: DISABLED**
**FIDELITY_EXECUTION: MANUAL_ONLY**
**ALPACA: MARKET_DATA_ONLY**

---

## 1. Executive Summary

This is a controlled amendment to the PAPER_TRADING_V1.0 freeze
(`STEP_22_FREEZE_REPORT.md`, commit `ca86e33`, tag `paper-trading-v1.0`
— **preserved as-is, not modified or overwritten**). It adds Alpaca as
a **market-data-only** provider so the 90-day validation can use real
current U.S. equity and options market data (OPRA, when entitled)
without requiring an IBKR account, and re-freezes the result as
**PAPER_TRADING_V1.1**. The 90-day validation still has not started.

## 2. Starting State

- Prior frozen commit: `ca86e33fa07e9d04ee55ec9ee350e6a90f3f5532`
  (code) / `8cc06c83774b51f0ded72d11465453a17cd7ceb9` (manifest+report),
  tag `paper-trading-v1.0`.
- Verified via `make verify-freeze` before starting: all 28 checks
  passing, `VALIDATION NOT STARTED`.
- Confirmed dashboard's default data provider is `mock`; the only
  existing real-data integration is IBKR, which the owner does not
  currently have an account for.

## 3. Architecture Audit (Part 1)

Read and traced, before writing any code:

- `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `VALIDATION_MANIFEST.json`,
  `STEP_22_FREEZE_REPORT.md`.
- `src/data/provider.py` (`MarketDataProvider` ABC, canonical
  `StrictModel`/`TimestampedModel` base, `ensure_canonical`),
  `src/data/option_chain.py`, `src/data/quotes.py`,
  `src/data/historical.py`, `src/brokers/ibkr.py` (as the template for
  a real provider's config/retry pattern), `src/risk/`,
  `config/risk_limits.yaml`, `config/brokers.yaml`, `.env.example`.
- Dashboard startup and data-loading paths (`src/dashboard/app.py`,
  `service.py`).
- **Key findings, not assumed:**
  1. `app/` (a separate, very early `app/data/mock_provider.py`/
     `app/config.py` prototype) is dead code — nothing under `src/`
     imports it, and `scripts/start.sh` runs `src.dashboard.app:app`
     exclusively. Documented in `ARCHITECTURE.md`, left in place
     (removing unrelated dead code was out of scope).
  2. **No production code path anywhere selected a market-data
     provider at runtime before this amendment.** `IBKRBroker`
     (`src/brokers/ibkr.py`) is a full, tested adapter, but
     `src.dashboard.service`/`src.workflows.feed_health` both take
     already-fetched `OptionChain`s as plain parameters (their own
     docstrings: "this module has no market-data connection of its
     own"), and `/morning-scan` is a Claude Code skill whose runner
     constructs and calls a provider directly. `src/data/factory.py`
     (new) is the first explicit `OPTIONS_AGENT_DATA_PROVIDER ->
     provider instance` construction point in this codebase.
  3. `src.data.provider.OptionContract`/`UnderlyingQuote`'s `source`
     field is a deliberately open string (not a closed enum), exactly
     so a new provider/feed distinction doesn't require a canonical-
     schema change — reused for OPRA-vs-indicative labeling rather
     than adding a new field.

## 4. Alpaca Is Market-Data-Only (Parts 2, 13-15)

`AlpacaMarketDataProvider` (`src/data/alpaca_provider.py`) implements
only `src.data.provider.MarketDataProvider`'s two read methods
(`get_option_chain`, `get_underlying_quote`). It is not a `Broker`
subclass, defines no order-submission/cancellation/modification method
of any kind, and never imports `alpaca.trading` (Alpaca's separate
order-submission client) anywhere.

**Enforced, not just documented** — `tests/acceptance/
test_alpaca_market_data_only.py` (20 tests):
- A repo-wide regex grep (not just the two Alpaca modules) proving no
  `from/import alpaca.trading` statement exists anywhere in `src/` or
  the repository.
- `AlpacaMarketDataProvider`'s public surface is exactly
  `MarketDataProvider`'s own contract — no extra method.
- `config/brokers.yaml` does not list `alpaca` as an execution broker;
  Fidelity's `execution_mode` is still `MANUAL`.
- `BrokerEnvironment` still has exactly one member (`PAPER`).
- `src.risk.engine` never references Alpaca at all.
- No credential value is ever formatted into a log call or an
  exception message (only the environment-variable *names* are).
- A socket-patched runtime test: with a fake client injected (the only
  way tests run), `get_option_chain` genuinely never opens a socket.

`src/validation/freeze.py`'s `verify_freeze()` now also runs
`_verify_alpaca_is_market_data_only()` directly (not just via the
test suite) on every freeze verification — an independent, always-on
re-check of the identical `alpaca.trading` import-grep guarantee.

## 5. Official Alpaca API/SDK (Part 3)

**SDK:** `alpaca-py` (https://github.com/alpacahq/alpaca-py), pinned
`alpaca-py==0.44.0` in `requirements.txt` — the current version
confirmed directly from PyPI at implementation time (the SDK's own
documentation site, `alpaca.markets`, is not reachable from this
network's egress policy; the GitHub source and PyPI package page were
used instead to verify current class/method/field names directly
against the installed package, not from memory).

**Endpoints/methods actually called** (verified against installed
`alpaca-py` source, not assumed):
- `alpaca.data.historical.stock.StockHistoricalDataClient
  .get_stock_latest_quote` (`StockLatestQuoteRequest`) — underlying
  quote.
- `alpaca.data.historical.option.OptionHistoricalDataClient
  .get_option_chain` (`OptionChainRequest`) — full option chain
  snapshot (quote, trade, IV, greeks per contract).
- `alpaca.data.historical.stock.StockHistoricalDataClient
  .get_stock_bars` (`StockBarsRequest`) — the separate, minimal
  historical-bars adapter (`alpaca_historical.py`).
No other Alpaca client or endpoint is called anywhere in this
amendment. `alpaca.data.enums.OptionsFeed` (`OPRA`/`INDICATIVE`) and
`DataFeed` (`IEX`/`SIP`/`DELAYED_SIP`/...) — confirmed exact member
names/values directly from the installed package.

## 6. Canonical Option Data (Part 4)

Every field the governing instruction names (`underlying`,
`option_symbol`, `expiration`, `strike`, `right`, `bid`, `ask`, `last`,
`volume`, `open_interest`, `iv`, `delta`/`gamma`/`theta`/`vega`,
`underlying_price`, `timestamp`, `source`) is explicitly mapped from
Alpaca's `OptionsSnapshot`/`Quote`/`Trade` response objects — never a
raw Alpaca object crosses the boundary (`ensure_canonical` is tested
directly against the provider's real output). Missing/unavailable
fields (Alpaca reports no `volume`/`open_interest` on an options
snapshot) are explicit `0`, never fabricated; a missing quote or a
crossed market (`bid > ask`) causes that single contract to be
**skipped**, never invented or silently coerced into validity. IV/
Greeks come from Alpaca's own reported values (this platform's
existing invariant — an LLM never computes these; the deterministic
Quant Engine, unchanged by this amendment, is the authoritative
cross-check when one is needed for a risk decision).

## 7. OPRA vs Indicative Data (Part 5)

`AlpacaConfig.options_feed` (`"opra"`/`"indicative"`) is explicit,
validated config — the provider requests exactly the configured feed
and never silently substitutes the other. If Alpaca's API rejects the
request because the account isn't entitled,
`AlpacaFeedEntitlementError` raises immediately (fail closed). Every
canonical `OptionContract`/`UnderlyingQuote`'s own `source` field
records exactly which feed served it (`alpaca_opra`,
`alpaca_indicative`, `alpaca_sip`, `alpaca_iex`). The provider-health
check (`src/data/provider_health.py`) surfaces `opra_entitled` and
`options_feed_type` explicitly — never silently falls back to mock or
to a different feed than configured, and reports `REAL_DATA_UNAVAILABLE`
rather than fabricating data when the configured feed can't be reached.
`VALIDATION_MANIFEST.json` records `required_options_feed_for_validation:
"opra"` as an explicit policy statement for the formal run.

## 8. Freshness (Part 6)

No new freshness mechanism was needed — every canonical `OptionContract`/
`UnderlyingQuote` Alpaca produces is a `TimestampedModel` (via
`src.data.option_chain`/`quotes`), so the pre-existing
`freshness_status`/`require_fresh`/`assert_tradable` machinery (age
checked against `DEFAULT_MAX_QUOTE_AGE`, the same choke-point every
other provider already goes through) applies identically and without
modification. `src/data/market_calendar.py` (Step 22) is reused
unmodified by `provider_health.py` for the market-open/closed state.

## 9. Configuration (Part 7)

`OPTIONS_AGENT_DATA_PROVIDER` (`mock`/`ibkr`/`alpaca`, default `mock`),
`OPTIONS_AGENT_ALPACA_API_KEY`, `OPTIONS_AGENT_ALPACA_API_SECRET`,
`OPTIONS_AGENT_ALPACA_OPTIONS_FEED` (default `opra`),
`OPTIONS_AGENT_ALPACA_STOCK_FEED` (default `sip`) — all in
`.env.example` with blank placeholders and comments, matching this
codebase's existing `OPTIONS_AGENT_*` naming convention. No default
credential value exists anywhere. `src/data/factory.py`'s
`get_configured_market_data_provider` fails closed (raises
`DataProviderConfigError`) on a missing/invalid real-provider
configuration — it never silently falls back to `mock`.

## 10. Provider Health Check (Part 8)

`src/data/provider_health.py`'s `check_provider_health` reports:
provider selected, authenticated (yes/no/n·a for mock), equity data
availability, options data availability, feed type, OPRA entitlement
(when determinable), market open/closed, last successful fetch,
equity-quote age, and every error encountered — never silently
swallowed. Surfaced via `GET /api/data-provider-health` (new, read-
only dashboard route) and a new "Market Data" panel at the top of the
dashboard showing REAL DATA CONNECTED / MOCK DATA / REAL DATA
UNAVAILABLE and OPRA vs indicative/delayed, so the owner never has to
guess what kind of data is powering the system.

## 11. Option Chain / Underlying Quotes / Historical Data (Parts 9-11)

- `get_option_chain`: handles calls/puts, multiple expirations/strikes,
  bid/ask/last/IV/greeks, empty chains, malformed OCC keys (skipped),
  a mismatched-underlying-root defense-in-depth check, and duplicate
  keys (not possible — Alpaca's response is itself a dict keyed by
  symbol). Pagination is handled internally by `alpaca-py`'s own
  client (`get_option_chain`'s documented page-limit handling); this
  module makes one logical call per chain fetch.
- `get_underlying_quote`: symbol, bid, ask, a computed `last` (bid/ask
  midpoint — Alpaca's latest-quote endpoint carries no separate trade
  price), timestamp (made timezone-aware if the SDK ever returns a
  naive one), source/feed.
- `AlpacaHistoricalDataProvider.get_bars`: audited first (§3, finding
  2) — nothing in `src.backtest`/`src.validation` currently calls a
  live `HistoricalDataProvider`; this is the minimal adapter for a
  future caller, not a redesign of the existing (fixture-driven)
  benchmark/regime infrastructure.

## 12. Rate Limiting, Retries, Error Handling (Part 12)

`AlpacaMarketDataProvider._with_retry` retries a *transient* failure
(timeout, connection error, an unrecognized error) up to
`max_retries` (default 3) with exponential backoff. Authentication
(`AlpacaAuthenticationError`) and feed-entitlement
(`AlpacaFeedEntitlementError`) failures are **never retried** — retrying
a 401/403 cannot succeed and would only delay surfacing a real
configuration problem. Exhausting retries raises the classified error
(`AlpacaRateLimitError` for a persistent 429, or a generic
`ProviderError` otherwise) — a persistent failure never becomes
fabricated data.

## 13. PaperBroker Remains Execution Authority (Part 13)

Verified explicitly, not assumed: `data_provider=alpaca` changes only
where a quote comes from. `tests/acceptance/
test_alpaca_market_data_only.py::TestAlpacaNeverTouchesFidelityOrPaperBrokerOrRiskAuthority`
proves `config/brokers.yaml` never gained an `alpaca` execution entry,
Fidelity's `execution_mode` is unchanged, `BrokerEnvironment` still has
exactly one member, and neither Alpaca module imports
`src.brokers.paper`/`src.brokers.fidelity` at all — there is no code
path connecting an Alpaca-sourced quote to anything but the same
canonical `OptionChain`/`UnderlyingQuote` every other provider already
produces.

## 14. Fidelity Remains Manual (Part 14)

Unchanged. No Fidelity login automation, credential storage, scraping,
browser automation, order submission, auto-clicking, or unofficial
Fidelity API exists — this amendment touches nothing under
`src/brokers/fidelity.py`, and the existing FS-001 through FS-005
regression tests (Steps 17/22) all still pass unmodified.

## 15. Risk Engine Remains Final Veto (Part 15)

Unchanged. `src.risk.engine` was not modified by this amendment and
was verified to never reference Alpaca at all — stale data, incomplete
pricing, invalid chains, uncalculable max loss, excessive size/
concentration/drawdown, insufficient buying power, invalid structures,
and missing required information all still fail closed exactly as
before, because Alpaca output is the same canonical
`OptionChain`/`OptionContract`/`UnderlyingQuote` types the Risk Engine
already consumes from every other provider.

## 16. Dashboard (Part 16)

Minimal, additive change: a new "Market Data" panel (above the
existing Portfolio panel) showing provider, authenticated, equity/
options data availability, options feed (labeled "OPRA (real)" /
"indicative (free/delayed)" / "mock (synthetic)"), market open/closed,
and last successful fetch — backed by the new read-only `GET
/api/data-provider-health` route. No live-trade button was introduced;
the existing route allowlist test
(`tests/unit/dashboard/test_app_security.py
::TestNoExecutionShapedRoute::test_every_registered_route_is_on_the_explicit_allowlist`)
was updated to include exactly this one new GET route and continues to
fail loudly on anything execution-shaped.

## 17. Testing (Part 17)

| File | Tests |
|---|---|
| `tests/unit/data/test_alpaca_provider.py` | 42 |
| `tests/unit/data/test_alpaca_historical.py` | 6 |
| `tests/unit/data/test_factory.py` | 8 |
| `tests/unit/data/test_provider_health.py` | 4 |
| `tests/acceptance/test_alpaca_market_data_only.py` | 20 |
| `tests/unit/dashboard/test_app_routes.py` (new case) | 1 |
| `tests/unit/validation/test_freeze.py` (new cases) | 3 |

Covers: authentication configuration/missing credentials, underlying/
option-chain mapping, calls/puts, multiple expirations, greeks present/
absent, IV present/absent, open interest (always 0, documented), zero
bid/zero ask, crossed markets (skipped), stale-data compatibility
(inherited `TimestampedModel` freshness, unmodified), malformed
timestamps (made timezone-aware), malformed OCC keys (skipped),
mismatched-underlying keys (skipped), API timeout/rate-limit/
entitlement/auth errors (classified, never retried when unretryable),
provider unavailable, no contracts (empty chain), canonical-schema
rejection (crossed market skipped, not raised past the boundary),
Risk-Engine/PaperBroker/Fidelity untouched, and — critically — no
Alpaca order-submission path exists anywhere. Every test uses a fake/
injected client; none requires real Alpaca credentials.

Every test in this amendment uses mocked SDK response objects built
via `Model.model_construct(...)` against the actually-installed
`alpaca-py` package's real classes (`Quote`, `Trade`, `OptionsSnapshot`,
`OptionsGreeks`, `Bar`) — field names were verified directly against
the installed package (`model_fields`), not assumed from documentation.

## 18. Security Review (Part 18)

Hostile-review pass, explicitly checking for: credential leakage (none
— grepped for `print`/`log*` calls referencing `api_key`/`api_secret`;
none found; a direct test constructs an `AlpacaAuthenticationError`
with a fake secret value and asserts the secret never appears in the
exception text), secrets in logs (none), secrets committed to git (none
— no `.env` file exists, `.gitignore` unchanged), order-submission
methods (none — enforced by the public-surface-equals-
`MarketDataProvider` test), trading API imports (none, repo-wide
grepped), hidden execution paths (none), mock fallback (none — a
misconfigured real provider raises, never substitutes mock), fail-open
behavior (none — reviewed every `except` in the four new modules;
none silently passes/succeeds on an error), raw provider objects
crossing the canonical boundary (none — `ensure_canonical` tested
directly against real provider output), LLM ability to influence
provider credentials/mapping (none — `src/llm/` has zero references to
Alpaca), unsafe exception handling (none — every classified exception
either raises or is recorded as an explicit error string, never
swallowed).

**No CRITICAL or HIGH issue found.**

## 19. Full Regression (Part 19)

```
2492 passed, 4 skipped, 0 failed, 2 warnings (both third-party deprecation notices)
```

84 new tests were added by this amendment (42 + 6 + 8 + 4 + 20 across
the 5 new/dedicated Alpaca test files, plus 1 new dashboard-route case
and 3 new freeze-manifest cases — each count independently confirmed
via `pytest --collect-only`). The 4 skips are the same pre-existing,
documented false positives, unrelated to this amendment. No test was
deleted, weakened, or bypassed.

## 20. Pre-Validation Re-Freeze (Part 20)

**Original V1.0 artifacts preserved, not overwritten**: `STEP_22_FREEZE_REPORT.md`
remains unchanged at the repository root; the `paper-trading-v1.0` git
tag remains untouched, still pointing at commit
`8cc06c83774b51f0ded72d11465453a17cd7ceb9`. This report
(`STEP_22_1_FREEZE_REPORT.md`) and a regenerated `VALIDATION_MANIFEST.json`
(now `PAPER_TRADING_V1.1`, manifest schema version `1.1.0`) are new,
separate artifacts documenting the amendment.

`VALIDATION_MANIFEST.json` now additionally records:
`freeze_version` (`"1.1"`), `alpaca_provider_module_hash`,
`data_provider_at_freeze_time` (what `OPTIONS_AGENT_DATA_PROVIDER` was
configured to at freeze time — `mock`, since this freeze happened
without live credentials configured; an operator may still select
`ibkr`/`alpaca` at runtime, which is a legitimate operational choice,
not a drift from this frozen artifact), and
`required_options_feed_for_validation` (`"opra"`, the policy statement
for the formal run) — alongside every field the original V1.0 manifest
carried (all unchanged in shape). `verify_freeze()` now runs 29 checks,
including a new always-on structural re-check
(`_verify_alpaca_is_market_data_only`) independent of the test suite.

## 21. Git Commit, Tag, and Manifest Hash (Part 21)

- **Git commit (code amendment):** `a33c7ba7358e31d66463252097ae9a5ea26d0a8f`
  — "Step 22.1: add Alpaca as a market-data-ONLY provider (pre-validation
  amendment)". `VALIDATION_MANIFEST.json`'s own `git_commit` field
  records exactly this SHA (generated immediately after this commit,
  against a clean working tree — `repository_state: "clean"`).
- **Git commit (this report + manifest + progress log):** the commit
  that immediately follows this one in `git log` — necessarily one
  commit after the code commit above, for the same reason as V1.0 (a
  manifest has to describe a tree before it can be added to that tree).
  See `progress.md`'s Step 22.1 entry for that exact SHA.
- **Git tag:** `paper-trading-v1.1`, applied to that follow-up commit.
  **Verification (not merely claimed):**
  ```
  $ git rev-parse paper-trading-v1.1^{commit}
  <shown in progress.md and the final plain-English report>
  $ git tag -l -n1 paper-trading-v1.1
  <shown in progress.md and the final plain-English report>
  ```
  **Remote push:** the branch itself was pushed successfully; pushing
  the `paper-trading-v1.0` tag in the prior step failed with an HTTP
  403 (a permission-scope restriction on this session's git
  credentials, not a network error — confirmed by retrying). The same
  restriction is expected to apply to `paper-trading-v1.1`; this report
  states plainly whether the push succeeded or failed rather than
  assuming success — see the final plain-English report for the actual
  result.
- **Manifest hash:** `62bef508f4aa99996bd84a5b3b044bab4dca3b49e3f8e65cd7937cd91050f0d9`

## 22. Confirmations

- **Fidelity remains MANUAL ONLY.** Unchanged; all pre-existing FS-00x
  tests pass unmodified.
- **Alpaca is MARKET DATA ONLY.** Enforced by 20 dedicated acceptance
  tests plus a standing `verify_freeze()` check — never execution.
- **PaperBroker remains the sole simulation execution destination.**
  Unchanged; no code path connects Alpaca data to any execution broker.
- **Risk Engine remains final veto.** Unchanged; `src.risk.engine`
  never references Alpaca.
- **Live trading is disabled.** `BrokerEnvironment` still has exactly
  one member (`PAPER`); `VALIDATION_MANIFEST.json`'s
  `live_trading_enabled` is `false`.
- **The 90-day validation has NOT started.** No cohort was created, no
  Day 1 snapshot recorded, starting NAV untouched, no scheduling
  enabled. `VALIDATION_MANIFEST.json`'s `validation_cohort_started` is
  `false`.

---

**PAPER_TRADING_V1.1: FROZEN**
**90_DAY_VALIDATION: NOT_STARTED**
**LIVE_TRADING: DISABLED**
**FIDELITY_EXECUTION: MANUAL_ONLY**
**ALPACA: MARKET_DATA_ONLY**
