# SECURITY_AUDIT.md

## Hostile System Audit — Step 17

**Posture of this document:** this audit was performed as if by an independent
team hired to find every way this options-trading research platform could
lose money incorrectly, calculate risk incorrectly, corrupt accounting,
hallucinate data, bypass safeguards, or execute unintended trades.

**Step 17B remediation update:** every CRITICAL and HIGH finding (10 total —
OP-001, SY-001, SY-002, SY-004, MD-001, MD-003, SY-003, SY-005, SY-006,
TS-004) has since been fixed, each with a regression test proving the
vulnerability is closed, and is marked **Status: FIXED (Step 17B)** in its
own section below with a pointer to the fix and its test. No existing test
was weakened to reach a passing state; the full suite (1531 tests) passes.

**Step 22 remediation update:** as part of pre-validation hardening (Step
22 Part 17), two of the remaining MEDIUM/LOW findings were closed with
low-risk, defense-in-depth fixes, each with new regression tests and zero
change to existing passing-test behavior: **OP-003** (a new
`PlaceOrderRequest` model validator in `src/brokers/base.py` now requires
every leg's quantity to be an exact integer multiple of the smallest leg's
quantity, matching one of this platform's own defined strategy ratios) and
**FS-005** (a new `FidelityTradeTicket` model validator in
`src/brokers/fidelity.py` now requires direct construction to be at
`AWAITING_HUMAN`, exploiting the fact that pydantic v2's `model_copy()` —
what `transition()`/`confirm_fill()` exclusively use — never re-runs
`@model_validator` hooks, so the legitimate state machine is unaffected).
Both are marked **Status: FIXED (Step 22)** below. All other MEDIUM/LOW
findings remain **OPEN** and unremediated, per Step 22's own explicit
"do not change working architecture unnecessarily" instruction — this
document still doubles as the audit record for those.

**Method:** four parallel, independent, read-only investigations (market-data
failures; options mechanics; system/orchestration failures; LLM boundary +
quantitative formula correctness) plus direct manual review of the Risk
Engine, drawdown/kill-switch/concentration/correlation modules, the Fidelity
manual-execution boundary, and the trade state machine. Every finding below
was verified against the actual source at the cited file:line — none are
speculative. Severities follow standard CRITICAL / HIGH / MEDIUM / LOW usage:
CRITICAL = live path to real financial harm or unauthorized action; HIGH =
serious correctness/safety defect on a reachable path; MEDIUM = real defect,
narrower reach or capped consequence; LOW = real but minor, cosmetic, or
purely theoretical today.

**Scope note:** this is a *research and paper-trading* platform. No LIVE
broker execution mode exists anywhere in the codebase (verified below).
Severities are assessed against what the platform actually does today
(paper trading, and generating human-reviewed Fidelity tickets for manual
entry) — several findings would become materially more severe if the
platform were ever connected to real capital without remediation first.

---

## Summary table

| ID | Severity | Status | Section | One-line summary |
|----|----------|--------|---------|-------------------|
| OP-001 | CRITICAL | **FIXED** | Options | Backtest assignment/exercise drops the share-value side of settlement, corrupting P&L by the full strike notional |
| SY-001 | CRITICAL | **FIXED** | System | Screener-generated `proposal_id`s are unique only within one scan call; reused across scans they collide and silently swallow real new trades |
| SY-002 | CRITICAL | **FIXED** | System | All idempotency/database/audit state is in-memory only; a crash-then-retry after a real fill cannot be recognized and will double-submit |
| SY-004 | CRITICAL | **FIXED** | System | Sequential candidates in one `/morning-scan` run all check against the same pre-scan portfolio snapshot; risk limits are not enforced cumulatively within a batch |
| MD-001 | HIGH | **FIXED** | Market-data | The Risk Engine's freshness gate is bypassed by simply omitting the `now` parameter (falls back to the proposal's own self-reported time, not the wall clock) |
| MD-003 | HIGH | **FIXED** | Market-data | Contract-matching never verifies the underlying ticker, only (expiration, strike, right) |
| SY-003 | HIGH | **FIXED** | System | An exception in the Portfolio-update pipeline stage is uncaught; a real fill can permanently never reach the database |
| SY-005 | HIGH | **FIXED** | System | `Portfolio.cash` is never updated after a fill — only positions are appended |
| SY-006 | HIGH | **FIXED** | System | Reconciliation discrepancies are only ever reported, never used to correct state or block a duplicate recommendation |
| TS-004 | HIGH | **FIXED** | Trade State | A CLOSE/ROLL proposal the Risk Engine approves has no order artifact built for it; the Order Validator crashes with an uncaught `AttributeError` |
| MD-002 | MEDIUM | OPEN | Market-data | Quant Engine (pipeline stage 1) skips the ticker-match and liquidity checks the Risk Engine performs on the same data |
| MD-004 | MEDIUM | OPEN | Market-data | `OptionChain` has no validator enforcing every contract's `underlying` matches the chain's own symbol |
| MD-005 | MEDIUM | OPEN | Market-data | `UnderlyingQuote` has no bid≤ask validator (unlike `OptionContract`/`HistoricalOptionQuote`) |
| MD-006 | MEDIUM | OPEN | Market-data | A zero-contract chain fetch is reported "healthy" by feed-health verification |
| OP-002 | MEDIUM | OPEN | Options | `PaperBroker` never re-marks an assigned equity position to the live underlying price |
| OP-003 | MEDIUM | **FIXED (Step 22)** | Options | Multi-leg order quantity has no cross-leg equality validator; only leg 0's quantity is consulted |
| OP-005 | MEDIUM | OPEN | Options | "Dividend risk" / "early exercise" are mandatory checklist categories with zero grounding data anywhere |
| SY-007 | MEDIUM | OPEN | System | The Order Validator's own duplicate-id check is dead code — never supplied a non-empty set by its only caller |
| SY-008 | MEDIUM | OPEN | System | Fidelity execution time-of-day bucketing silently assumes a timezone nothing enforces |
| LM-001 | MEDIUM | OPEN | LLM | Devil's Advocate's `why_not_thesis` text is re-embedded, unsanitized, into the Portfolio Manager's next LLM call |
| LM-002 | MEDIUM | OPEN | LLM | Automated "no numeric field" tests cover only 3 of 15 non-`TradeProposal` LLM schemas |
| QF-001 | MEDIUM | OPEN | Quantitative | Portfolio correlation check is silently skipped (not fail-closed) whenever `price_history` is empty |
| MD-007 | LOW | OPEN | Market-data | Strike/expiration matching fails closed (over-rejects) rather than mismatching; corporate-action sub-cent strikes could trigger it |
| MD-008 | LOW | OPEN | Market-data | Corporate actions/stock splits entirely unhandled; contract multiplier hardcoded as 100 in 8+ places |
| OP-004 | LOW | OPEN | Options | Put-credit-spread collateral netting is scoped to a single order (fails safe, over-conservative) |
| OP-006 | LOW | OPEN | Options | Exact-at-the-money settlement deterministically resolves to "not assigned"; pin risk is unmodeled and undocumented |
| OP-007 | LOW | OPEN | Options | The 100x contract multiplier is redeclared independently in 8+ modules |
| SY-009 | LOW | OPEN | System | No market-holiday calendar exists anywhere; DTE/trading-day math is raw calendar days |
| SY-010 | LOW | OPEN | System | No locking anywhere; current race-freedom is incidental to an all-synchronous implementation, not a designed guarantee |
| LM-003 | LOW | OPEN | LLM | `profit_target`/`management_dte` reach the human-facing Fidelity ticket unchecked beyond schema bounds (currently a dormant path) |
| LM-004 | LOW | OPEN | LLM | `PortfolioManagerReview` is unused, untested dead code shadowing `PortfolioDecision` |
| FS-005 | LOW | **FIXED (Step 22)** | Fidelity | `FidelityTradeTicket` can in principle be constructed directly at a terminal FILLED status, bypassing the lifecycle functions (no live exploit path) |
| QF-002 | LOW | OPEN | Quantitative | Survivorship bias in backtesting is honestly documented as unenforced ("where possible"), not actually prevented |
| QF-003 | LOW | OPEN | Quantitative | `TRADING_DAYS_PER_YEAR` is a dead constant in `src/backtest/metrics.py` that could mislead a future maintainer |

Also see **"Verified safe / no finding"** at the end of each section and the
consolidated list at the very end — a large majority of the codebase's own
stated safety invariants were checked and held.

---

## QUANTITATIVE FAILURES

### QF-001 — Portfolio correlation check silently skipped when price history is empty
- **Severity:** MEDIUM
- **Component:** `src/risk/correlation.py::check_correlation` (lines 31-51)
- **Description:** `check_correlation` requires `Portfolio.price_history` to already contain aligned price series for every ticker being compared. When it's empty (true for essentially every `Portfolio` in this codebase today — no live wiring populates it), the function returns without raising, i.e. the correlation check passes by default rather than failing closed. This is explicitly acknowledged in the module's own docstring as "a deliberate, documented gap, not an oversight" — included here because the audit's job is to surface it as a live risk-control gap regardless of whether it's already documented.
- **Possible consequence:** a portfolio can accumulate multiple highly-correlated concentrated bets (e.g. several tech-sector puts on different tickers that move together) without the correlation gate ever actually firing, defeating one of the Risk Engine's stated portfolio-level protections.
- **Reproduction:** construct any `Portfolio` with two or more positions and an empty `price_history` dict (the default), then call `check_correlation(portfolio, new_ticker, limits)` — it returns `None` (no error) regardless of how correlated `new_ticker` would be with existing holdings.
- **Recommended remediation (not applied):** either wire real historical price data into `Portfolio.price_history` before this check can be trusted, or make the absence of price history a fail-closed `CorrelationError` (with an explicit override for cases where correlation genuinely cannot be assessed) rather than a silent pass — consistent with the codebase's own "fail closed" principle stated in `src/risk/engine.py`'s module docstring.

### QF-002 — Survivorship bias in backtesting is documented but not prevented
- **Severity:** LOW (informational)
- **Component:** `src/backtest/engine.py` module docstring; `src/backtest/simulator.py::HistoricalOptionChainProvider`
- **Description:** The backtest engine's own docstring states survivorship bias is preventable only "where possible," since it depends on whichever real historical data vendor eventually implements `HistoricalOptionChainProvider` — no such vendor exists yet. This is honestly disclosed, not hidden, but it means any backtest run today (necessarily against synthetic/test data) carries no actual survivorship-bias protection.
- **Possible consequence:** once a real vendor is wired in, if that vendor silently omits delisted/failed tickers, backtested returns would be systematically overstated with no mechanism in this codebase to detect it.
- **Reproduction:** N/A — this is a structural gap pending a real vendor decision, not a triggerable bug in current code.
- **Recommended remediation (not applied):** when a real historical vendor is selected, verify explicitly that it includes delisted/failed securities, and add a test that fails if a known-delisted ticker is silently absent from a fetched historical universe.

### QF-003 — Dead `TRADING_DAYS_PER_YEAR` constant in metrics.py
- **Severity:** LOW (cosmetic)
- **Component:** `src/backtest/metrics.py:17`
- **Description:** `TRADING_DAYS_PER_YEAR = 252.0` is declared but never referenced anywhere else in the module or codebase (verified by repo-wide search). All annualization in this module (`cagr`, `sharpe_ratio`, `sortino_ratio`, `annual_volatility`) instead derives `years`/`periods_per_year` from the actual calendar-day span of the equity curve, not from this constant.
- **Possible consequence:** none today (no live computation depends on it) — but a future maintainer reading this file could reasonably believe metrics use a 252-trading-day convention when they actually self-derive periods empirically from calendar days, leading to a wrong mental model when debugging or extending this module.
- **Reproduction:** `grep -rn "TRADING_DAYS_PER_YEAR" src/` returns only its own declaration line.
- **Recommended remediation (not applied):** either remove the constant, or wire it in intentionally (e.g. as an explicit alternate annualization mode) and document which convention is actually in effect.

**Note:** Black-Scholes core pricing (`src/quant/black_scholes.py`), probability
of profit (`src/quant/probability.py`), the max profit/max loss/breakeven
formulas for all three approved strategies (`src/quant/expected_value.py`),
and `max_drawdown`/`longest_drawdown_days` (`src/backtest/metrics.py`) were
all spot-checked against textbook definitions and found correct — see
"Verified safe" at the end of this document. The single largest quantitative
correctness defect found in this audit is a backtest-engine bug, documented
as **OP-001** below since it is fundamentally about assignment/exercise
mechanics.

---

## MARKET-DATA FAILURES

### MD-001 — Risk Engine's freshness gate is bypassed by omitting `now`
- **Severity:** HIGH
- **Component:** `src/risk/engine.py:104-113` (`evaluate_trade_proposal` signature), `:157` (`as_of = now or proposal.timestamp`)
- **Description:** `evaluate_trade_proposal`, the codebase's own documented "single deterministic choke point every trade must pass through," accepts an optional `now: datetime | None = None`. When omitted, `as_of` falls back to `proposal.timestamp` — the proposal's *own self-reported* timestamp, never the real wall clock. No independent clock is consulted anywhere in this call chain (repo-wide search: the only `datetime.now()` call in all of `src/` is an unrelated `PaperBroker` default). Since `market_data.timestamp` and `proposal.timestamp` are normally stamped moments apart at proposal-construction time, `age ≈ 0` and `require_fresh` always reports "fresh" regardless of how much real time has actually elapsed since the data was fetched.
- **Possible consequence:** silent approval of a trade against arbitrarily stale market data — in the exact function whose entire job is to prevent that. A proposal built hours or days ago and only evaluated later (a queued review, a replay, a retried call) would pass the freshness gate every time.
- **Reproduction:** call `evaluate_trade_proposal(proposal, portfolio, qa, market_data, broker_caps, limits=limits)` without `now` — this is literally the calling convention the repository's own positive-control tests use (`tests/unit/risk/test_engine_positive_controls.py`, all APPROVE-path tests omit `now`). The one production call site (`src.orchestration.pipeline.run_order_pipeline`) is safe only because `PipelineRequest.now` is a required field — but `evaluate_trade_proposal` is a public module-level function, and any other caller (a manual review tool, a notebook, a future API endpoint, a retry path) that uses the natural default-parameter calling convention defeats the check with zero error or warning.
- **Recommended remediation (not applied):** make `now` a required keyword argument with no default (forcing every caller to supply the real wall clock explicitly), or have the function itself call a real clock (e.g. `datetime.now(timezone.utc)`) when `now` is not supplied, rather than falling back to data the proposal itself controls.
- **Status: FIXED (Step 17B).** Chose the first option: `now: datetime` is now a required keyword-only argument on both `evaluate_trade_proposal` and `_evaluate`, with no default and no fallback of any kind — `as_of = now` directly. A silent `datetime.now()` fallback was deliberately rejected as the fix, since it would make the function's behavior depend on the real wall clock even in tests that never intended that (a latent flakiness risk as this codebase's fixtures use fixed future dates). All test call sites that previously omitted `now` (relying on the vulnerable fallback) now supply it explicitly. Regression tests: `tests/unit/risk/test_engine_bypass_attempts.py::TestFreshnessGateCannotBeBypassedRegressionMD001` (proves `now` is mandatory, and that real elapsed time is caught even when the proposal's own timestamp is unchanged — the exact exploit shape).

### MD-002 — Quant Engine stage skips checks the Risk Engine performs on the same data
- **Severity:** MEDIUM
- **Component:** `src/orchestration/pipeline.py:103-127` (`default_quant_stage`) vs. `src/risk/engine.py:160-199`
- **Description:** `_evaluate` in the Risk Engine explicitly checks, before computing economics: (a) the market data's underlying ticker matches the proposal's, (b) the underlying quote is fresh, (c) every leg resolves to a live, fresh contract, (d) every leg is liquid. `default_quant_stage` — which runs *first* in the pipeline (stage 1, before Devil's Advocate and Portfolio Manager) and calls the identical `resolve_leg_contracts` — performs none of these checks itself.
- **Possible consequence:** no trade actually executes on bad data (the Risk Engine, running later, remains the real gate), but the pipeline manufactures and records a `QuantitativeAnalysis` — reviewed in full by both LLM stages and retained in `PipelineOutcome` even after final rejection — derived from potentially wrong-ticker or untradable-market data, with nothing indicating to those reviewers (human or LLM) that stage 1's numbers weren't gated the way stage 4's are.
- **Reproduction:** call `default_quant_stage` directly with a `market_data` chain for a different ticker than `proposal.ticker`, or with a contract whose bid/ask are both 0 but `last`/`iv` are populated — it returns a `QuantitativeAnalysis` without error in either case.
- **Recommended remediation (not applied):** have `default_quant_stage` call the same ticker-match and `check_all_legs_liquid` checks the Risk Engine uses (both already exist and are reusable), so intermediate pipeline artifacts are gated as strictly as the final decision.

### MD-003 — Contract-matching never verifies the underlying ticker
- **Severity:** HIGH
- **Component:** `src/risk/trade_risk.py:117-123` (`_find_contract`); `src/backtest/engine.py:109-118` (`_match_quotes_for_legs`)
- **Description:** Both functions match a contract/quote by `(expiration, strike, right)` only — neither checks that `contract.underlying` (or `quote.underlying`) actually equals the ticker being resolved for. The live-trading path is protected only because `src.risk.engine._evaluate` performs a separate, explicit `market_data.underlying.symbol != proposal.ticker` check *before* calling `resolve_leg_contracts` — but `default_quant_stage` (see MD-002) calls the identical `resolve_leg_contracts` with no such check, and `_find_contract`/`_match_quotes_for_legs` themselves have no internal safeguard at all. The backtest path has no equivalent check anywhere.
- **Possible consequence:** a caller bug that supplies a chain/quote set for the wrong ticker (e.g. `src/workflows/morning_scan.py`'s `fresh_chains` dict, keyed by ticker string but never cross-validated against `chain.underlying.symbol`) combined with a coincidentally-matching strike/expiration/right (very plausible for common round strikes and standard monthly expirations across large-caps) would silently price a trade — or a backtest fill — from a completely different underlying's market data, with no error at all.
- **Reproduction:** call `resolve_leg_contracts(proposal_for_TICKER_A, market_data_for_TICKER_B, ...)` where TICKER_B's chain happens to have a contract at the same (expiration, strike, right) as one of TICKER_A's proposed legs — it resolves successfully to TICKER_B's contract.
- **Recommended remediation (not applied):** add an explicit `underlying`/ticker equality assertion directly inside `_find_contract` and `_match_quotes_for_legs` themselves, so the guarantee doesn't depend on every caller having already checked it upstream.
- **Status: FIXED (Step 17B).** Both functions now take an explicit `underlying` parameter and match on `(underlying, expiration, strike, right)`, not just `(expiration, strike, right)` — `src.risk.trade_risk._find_contract`/`resolve_leg_contracts` (passes `proposal.ticker`) and `src.backtest.engine._match_quotes_for_legs` (passes `position.ticker`/`entry.ticker` at each of its three call sites). Regression tests: `tests/unit/risk/test_trade_risk_contract_matching.py::TestResolveLegContractsChecksUnderlying` and `tests/unit/backtest/test_engine.py::TestMatchQuotesForLegsChecksUnderlyingRegressionMD003` (both prove a wrong-ticker contract/quote at a coincidentally matching strike/expiration/right is never matched).

### MD-004 — `OptionChain` has no contract/underlying consistency validator
- **Severity:** MEDIUM
- **Component:** `src/data/option_chain.py:80-89`
- **Description:** `OptionChain` has `underlying: UnderlyingQuote` and `contracts: list[OptionContract]`, each contract carrying its own `underlying: str` field — but there is no `model_validator` enforcing every contract's `underlying` equals `self.underlying.symbol`.
- **Possible consequence:** a provider-adapter bug that merges contracts from two separate underlying fetches into one `OptionChain` object would construct a structurally "valid" chain (per Pydantic) mixing tickers, undetected by any canonical-model validation — feeding directly into MD-003's failure mode with an even smaller trigger (one malformed chain, not a caller-side dict-key mistake).
- **Reproduction:** `OptionChain(underlying=UnderlyingQuote(symbol="AAA", ...), contracts=[OptionContract(underlying="BBB", ...)], ...)` constructs without error.
- **Recommended remediation (not applied):** add a `model_validator` rejecting any contract whose `underlying` doesn't match `self.underlying.symbol`.

### MD-005 — `UnderlyingQuote` has no bid≤ask validator
- **Severity:** MEDIUM
- **Component:** `src/data/quotes.py:9-24`
- **Description:** `OptionContract` and `HistoricalOptionQuote` both explicitly reject `bid > ask` at construction. `UnderlyingQuote` has no equivalent validator — `bid`/`ask` are only independently constrained non-negative.
- **Possible consequence:** a crossed underlying quote (`bid=150, ask=100`) constructs successfully; `.mid` computes a plausible-looking but nonsensical value (125) with no error. This value is used directly in `src/workflows/candidate_generation.py` to decide which strikes count as OTM puts/calls, silently skewing candidate screening with no error surfaced anywhere.
- **Reproduction:** `UnderlyingQuote(symbol="XYZ", bid=150.0, ask=100.0, last=125.0, volume=1000, timestamp=..., source=...)` constructs without error; `.mid` returns 125.0.
- **Recommended remediation (not applied):** add the same `bid <= ask` `model_validator` `OptionContract` already has.

### MD-006 — A zero-contract chain is reported "healthy"
- **Severity:** MEDIUM
- **Component:** `src/workflows/feed_health.py:41-51` (`verify_market_data_feeds`)
- **Description:** Any successfully-returned `OptionChain`, including one with zero contracts, is reported `healthy=True`. Only an outright fetch exception is marked unhealthy.
- **Possible consequence:** no wrong trade results (an empty chain correctly produces zero candidates downstream — fails closed on the trading side), but the `/morning-scan` report's feed-health section says "all healthy" while silently screening zero candidates from a ticker whose feed may actually be broken (bad pagination, a parsing regression, an API contract change) — masking a real data problem from the human reviewing the report, with no way to distinguish "genuinely nothing to trade today" from "this feed is silently broken."
- **Reproduction:** supply `fetch_results={"XYZ": OptionChain(underlying=..., contracts=[], ...)}` to `verify_market_data_feeds` — returns `healthy=True, detail="ok (0 contract(s))"`.
- **Recommended remediation (not applied):** report a zero-contract chain as a distinct status (e.g. "empty," not "healthy"), and surface it distinctly in the rendered report.

### MD-007 — Wrong strike/expiration matching fails closed, but corporate-action strikes could over-reject
- **Severity:** LOW
- **Component:** `src/risk/trade_risk.py:119`; `src/backtest/engine.py:114`
- **Description:** All contract resolution is exact float/date equality, never fuzzy/nearest-match. Standard US equity strikes are exactly IEEE-754-representable, so this doesn't cause a *wrong* match — but a corporate-action-adjusted strike carrying an odd fractional cent (e.g. `$47.855`, a realistic post-special-dividend OCC adjustment) arriving from two different code paths with a sub-cent representation difference would fail to match at all.
- **Possible consequence:** an over-rejection (`ContractNotFoundError`/skip), not a mispriced trade — fails in the safe direction, but is a real availability gap for the (currently entirely unhandled, see MD-008) corporate-action case.
- **Reproduction:** N/A without a real corporate-action-adjusted contract in a test fixture; the equality-only matching logic itself is directly visible at the cited lines.
- **Recommended remediation (not applied):** if/when corporate-action-adjusted contracts are supported, match on a rounded/quantized strike rather than exact float equality.

### MD-008 — Corporate actions and stock splits are entirely unhandled
- **Severity:** LOW (documented gap)
- **Component:** platform-wide; `_CONTRACT_MULTIPLIER`/literal `100` in `src/workflows/candidate_generation.py:41`, `src/risk/trade_risk.py` (188, 196, 262), `src/brokers/paper.py:58`, `src/backtest/engine.py:73`, `src/backtest/assignment.py:18`, `src/workflows/rejected_trade_review.py:32`, `src/quant/greeks.py:83`, plus bare literals in `src/quant/expected_value.py` and `src/backtest/slippage.py`
- **Description:** No field for a non-standard contract multiplier exists anywhere in the type system (`OptionContract`, `HistoricalOptionQuote`); the multiplier is a hardcoded `100` independently redeclared in 8+ modules (all currently in agreement).
- **Possible consequence:** if a real adjusted contract (non-100 multiplier, or a cash-plus-shares deliverable, as OCC issues after some splits/spinoffs/special dividends) ever entered the system, every dollar computation (collateral, max loss, position sizing, P&L) using the hardcoded multiplier would be silently wrong by whatever factor the real multiplier differs from 100 — no validation, no error.
- **Reproduction:** N/A — this is an absence of a feature, not a triggerable defect in current test data.
- **Recommended remediation (not applied):** add a `multiplier` field to the canonical option-contract type (defaulting to 100), thread it through every dollar computation in place of the hardcoded literal, and reject/flag any contract whose multiplier isn't 100 until adjusted-contract handling is deliberately built.

---

## OPTIONS FAILURES

### OP-001 — Backtest assignment/exercise settlement drops the share-value side, corrupting P&L by the full strike notional
- **Severity:** CRITICAL
- **Component:** `src/backtest/engine.py:181-212` (`_settle_expired_position`); `src/workflows/rejected_trade_review.py:117-119` (`hypothetical_outcome_from_settlement`); root cause in how `src/backtest/assignment.py`'s output is consumed
- **Description:** `settle_leg`/`settle_position` (`src/backtest/assignment.py:37-58`) correctly compute *both* a `cash_impact` (the full strike-notional cash flow) and a `share_impact` (the resulting long/short stock position) for every assignment/exercise. `_settle_expired_position` consumes only `cash_impact` — `share_impact` is never read, and `src.backtest.simulator`'s `BacktestPosition`/`PortfolioState` have no share/equity ledger to apply it to even if it were read (`realistic_equity = state.realistic_cash` — equity is cash-only, forever). The identical bug is repeated in `rejected_trade_review.py`'s hypothetical-settlement path, built on the same primitive. By contrast, `src.brokers.paper.PaperBroker.settle_expiration` does this correctly (`self._update_position(underlying_symbol, share_delta, contract.strike, 1)`) — **only the backtest and rejected-trade-review paths are wrong; live/paper trading's own position book is right.**
- **Possible consequence:** any backtest of this platform's core strategies that experiences even one ITM assignment shows a P&L error on the order of the *full strike notional* (strike × 100 × contracts) — dwarfing premium, slippage, and commission effects the rest of the engine carefully tracks. Since `evaluate_target`/`build_backtest_result` grade the platform's 12-15% CAGR research target directly off these numbers, **the target evaluation is meaningless for any strategy/period that experiences an assignment** — which, for cash-secured puts and covered calls, is a routine, by-design outcome, not an edge case.
- **Reproduction:** this is codified as a currently-*passing* test that asserts the wrong number as correct — `tests/unit/backtest/test_engine.py::test_short_put_assigned_itm_at_expiration_reduces_pnl_by_intrinsic_value` (a cash-secured put, strike 95, sold for $200 credit, settling at $90 — only $500 ITM) asserts `realistic_pnl == -9,300.0`. The real economics: assignment costs $9,500 cash but delivers 100 shares worth ~$9,000 at settlement, for a true mark-to-market loss of ~$300 (premium $200 minus intrinsic $500) — the bug overstates the loss by ~$9,000, exactly the discarded share value. The mirrored covered-call test (`test_covered_call_assigned_itm_settles_via_the_short_call_leg_only`) asserts a $10,700 gain where the real gain is ~$700 — again overstated by exactly the discarded share value, and the test's own supplied `underlying_cost_basis=100.0` is never used in the P&L formula at all.
- **Recommended remediation (not applied):** either (a) give `BacktestPosition`/`PortfolioState` a real equity/share ledger and apply `share_impact` to it, marking the resulting shares to market and accounting for their eventual disposal, or (b) if share tracking is out of scope for this engine, compute a settlement P&L using *intrinsic value only* (not the full strike notional) and document explicitly that the backtest engine does not model post-assignment share economics. Update the two named tests once the fix is decided (they currently assert the bug's output as correct and must not be treated as passing evidence of correctness in the meantime).
- **Status: FIXED (Step 17B).** Implemented option (b): added `src.backtest.assignment.realized_settlement_pnl`, which converts `settle_position`'s per-leg output into an intrinsic-value-based economic impact — a covered position's own cost basis is used when a leg disposes of shares the caller already held (the only case `underlying_shares_held > 0` applies to), otherwise the new stock position is treated as immediately marked to the settlement price. `src.backtest.engine._settle_expired_position` and `src.workflows.rejected_trade_review.hypothetical_outcome_from_settlement` both now consume it instead of raw `cash_impact`. The two named tests (`test_short_put_assigned_itm_at_expiration_reduces_pnl_by_intrinsic_value`, `test_covered_call_assigned_itm_settles_via_the_short_call_leg_only`) and the rejected-trade-review equivalent were corrected to the true economics. Regression tests: `tests/unit/backtest/test_assignment.py::TestRealizedSettlementPnlRegressionOP001` (6 tests, including an explicit assertion that the old, wrong full-notional value is never produced).

### OP-002 — `PaperBroker` never re-marks an equity position to the live price after assignment
- **Severity:** MEDIUM
- **Component:** `src/brokers/paper.py:554-580` (`_update_position`), `:654` (assignment call site), `:314-328` (`get_account`/`get_positions`)
- **Description:** When a short option is assigned, the resulting stock position is booked via `_update_position(..., price=contract.strike, ...)` — marked at exactly the strike (zero unrealized P&L) at creation. Nothing ever re-marks it to the live underlying price afterward; `update_market_data` only stores the option chain, never touches `self._positions`. `get_account`/`get_positions` return the stored `market_value`/`unrealized_pnl` verbatim.
- **Possible consequence:** after an assignment, the reported net liquidation value and unrealized P&L for that equity position remain frozen at the strike price indefinitely, until some unrelated trade happens to touch that same symbol again — understating (or overstating) true net worth by exactly the unrecognized share price move in the meantime.
- **Reproduction:** assign a short put at strike 95 while the underlying is at 90 (100 shares booked at `market_price=95`), then let the underlying rally to 110 — `get_account()`/`get_positions()` still report that position's `unrealized_pnl` as 0 and `market_value` as `95*100`, not `110*100`.
- **Recommended remediation (not applied):** add a re-marking step (driven by `update_market_data` or an explicit `mark_to_market()` call) that reprices every held equity position to the current underlying quote before `get_account`/`get_positions` report it.

### OP-003 — Multi-leg order quantity has no cross-leg ratio-consistency validator
- **Severity:** MEDIUM (currently unreached in production)
- **Status: FIXED (Step 22)**
- **Component:** `src/brokers/base.py:61-73` (`OrderLeg`); `src/brokers/paper.py` (`base_combo_quantity`, `attempt_fill`, `_apply_fill`)
- **Step 21 update:** this finding's original description is now factually stale and has been corrected here rather than left to mislead a future reader (see `tests/acceptance/test_strategy_integrity.py::TestNoStaleEvaluationOnlyLanguage`, which exists for exactly this class of drift). As of Step 20A, `PaperBroker` no longer consults `order.legs[0].quantity` — it anchors on `base_combo_quantity` (the *minimum* per-leg quantity), and `LONG_CALL_BUTTERFLY`'s production call site legitimately constructs a non-uniform 1:-2:1 leg quantity ratio, which is correctly handled end to end (fill quantity, collateral, commission — see `tests/unit/brokers/test_paper_broker_multileg.py` and this Step's own `tests/acceptance/test_multileg_integrity.py`/`test_paper_broker.py`). The underlying gap the finding originally pointed at is narrower than first described, but still real: neither `OrderLeg` nor `PlaceOrderRequest` has a type-level validator confirming a *given* set of per-leg quantities forms one of the ratios the platform's strategy library actually defines (1:1:..., or 1:-2:1) — the only thing currently preventing a malformed ratio from reaching `PaperBroker` is that every current call site (`src.risk.engine._build_approved_order`) derives leg quantities correctly from an already-validated `TradeProposal`.
- **Possible consequence:** a future code path or data-entry bug that produced an arbitrary, non-strategy-shaped per-leg quantity mismatch (e.g. `[short qty=2, long qty=5]`, not any of this platform's own 16 named ratios) would still pass model validation and be silently absorbed as "2 combo units, with the qty=5 leg treated as 2x" rather than rejected.
- **Reproduction (pre-fix):** `PlaceOrderRequest(legs=[OrderLeg(..., quantity=2), OrderLeg(..., quantity=5)], ...)` constructed and filled without error, using `base_combo_quantity`=2 and silently over-filling the qty=5 leg's book-keeping relative to what a caller might have intended.
- **Fix (Step 22):** a new `@model_validator(mode="after")` on `PlaceOrderRequest` (`src/brokers/base.py`) computes each leg's ratio relative to the smallest leg's quantity, requires an exact integer multiple, and requires the resulting ratio set to be either uniform (1:1:...:1, any leg count) or exactly one 2 with the rest 1 on exactly 3 legs (the `long_call_butterfly` shape) — every other shape is rejected with a `ValidationError`. This is a no-op for every real call site (`src.risk.engine._build_approved_order` always derives an already-ratio-validated shape from `TradeProposal`), and purely closes the gap for any future/malformed caller.
- **Regression tests:** `tests/unit/brokers/test_base.py::TestPlaceOrderRequestLegRatioValidatorRegressionOP003` (8 tests: single-leg, uniform 2-leg, uniform 4-leg, 1:2:1 butterfly all accepted; non-integer-multiple, uneven 2-leg, two-legs-at-2x, and non-uniform 4-leg all rejected). Full suite re-verified passing after the fix (2388 tests, 4 skipped, 0 failed).

### OP-004 — Put-credit-spread collateral netting is scoped to a single order
- **Severity:** LOW
- **Component:** `src/brokers/paper.py:463-512` (`_validate_collateral`/`_required_collateral`) vs. `:582-609` (`_recompute_collateral`)
- **Description:** The pre-flight collateral check (`_validate_collateral`, run before a fill is attempted) only sees the legs in the *current* `PlaceOrderRequest` — it has no visibility into the existing position book. `_recompute_collateral`, run *after* a fill, does correctly net across the whole position book (explicitly built to handle a spread's two legs being opened in separate orders) — but by then the pre-flight gate has already made its (overly conservative) decision.
- **Possible consequence:** an account holding a long put from an earlier order, then submitting the matching short put as its own single-leg order, is charged the *full* cash-secured collateral amount on the pre-flight check rather than the narrower netted spread-width amount — an account with enough cash for the true (netted) requirement but not the full un-netted one is spuriously rejected. This fails safe (over-conservative), never under-collateralized.
- **Reproduction:** open a long put in one order, then submit a short put (that would form a credit spread with it) as a separate single-leg order with cash sufficient only for the netted spread width — `_validate_collateral` rejects it despite the position being properly covered after the fact.
- **Recommended remediation (not applied):** have `_validate_collateral` consult the existing position book (the same way `_recompute_collateral` already does) rather than only the current order's own legs.

### OP-005 — "Dividend risk" / "early exercise" checklist categories have no grounding data anywhere
- **Severity:** MEDIUM
- **Component:** `src/llm/schemas.py` (`RiskCategory` literal, `RiskCategoryAssessment`); `.claude/agents/devil_advocate.md`
- **Description:** `"dividend_risk"` and `"early_exercise"` are two of the Devil's Advocate's 18 mandatory risk categories — the schema only requires `applicable: bool` plus a free-text `note`, with no check on factual correctness. No dividend data source exists anywhere in the codebase (`src.quant.black_scholes`'s own docstring explicitly states pricing assumes no dividends and early-exercise/dividend risk is "a separate, documented v1 approximation... not folded into this pricing model" — i.e. acknowledged as unmodeled, not silently ignored at the pricing layer, but that same standard isn't extended to the Devil's Advocate's mandatory checklist item). No early-exercise probability model exists either.
- **Possible consequence:** for a covered call held through an ex-dividend date — the textbook case for early assignment to capture a dividend — this platform has no mechanism, deterministic or LLM-grounded, to actually detect the risk. The Devil's Advocate's note for this category is unavoidably an ungrounded guess, which is exactly the kind of fabricated-looking qualitative color the codebase explicitly rejects elsewhere (`FailureScenario`'s own docstring refuses fabricated numeric probabilities for the identical reason).
- **Reproduction:** inspect any `DevilsAdvocateReview.risk_assessment` entry for `category="dividend_risk"` — its `note` field has no underlying dividend-calendar data anywhere in this codebase to have been derived from.
- **Recommended remediation (not applied):** either wire a real dividend/ex-date data source into the Devil's Advocate's inputs (so the note can be grounded, the same way `earnings_in_window` is Python-resolved before the model sees it), or relabel this category's expectation to make clear it's an unavoidably qualitative, ungrounded judgment call rather than implying the same rigor as, say, `earnings_in_window`.

### OP-006 — Pin risk is unmodeled and undocumented
- **Severity:** LOW
- **Component:** `src/backtest/assignment.py:31-34` (`intrinsic_value`); `src/brokers/paper.py:632-636`
- **Description:** Settlement exactly at the strike returns `intrinsic_value == 0.0`, so the position is always treated as cleanly OTM/unassigned. No pin-risk concept (assignment uncertainty when settling very near the strike) exists anywhere. Unlike the European-exercise/no-dividends assumption in `black_scholes.py` (explicitly flagged in that module's own docstring as a known simplification), this one is a silent, undocumented assumption.
- **Possible consequence:** narrow real-world edge case (settlement exactly or very near the strike carries genuine broker/holder-dependent assignment uncertainty this platform always resolves one deterministic way) — not currently flagged anywhere as a simplification.
- **Reproduction:** `intrinsic_value(OptionRight.PUT, strike=100.0, settlement_price=100.0)` returns `0.0` deterministically.
- **Recommended remediation (not applied):** document this as an explicit, named simplification alongside the European-exercise assumption, even if no probabilistic pin-risk model is built.

### OP-007 — The 100x contract multiplier is redeclared in 8+ modules
- **Severity:** LOW
- **Component:** see MD-008's file list
- **Description:** Every one of the 8+ independent declarations currently agrees (all use 100 for standard equity options) — not a live bug — but there is no single source of truth. See MD-008 for the corporate-action angle on the same root cause.
- **Possible consequence:** a future change (e.g. supporting a non-standard multiplier) would require touching 8+ independent definitions; missing one would silently reintroduce a real cross-module mismatch (e.g. collateral computed with one multiplier, P&L with another).
- **Reproduction:** `grep -rn "_CONTRACT_MULTIPLIER = 100\|\* 100\b" src/` shows 8+ independent sites.
- **Recommended remediation (not applied):** consolidate into one shared, imported constant (or the `multiplier` field recommended in MD-008).

---

## SYSTEM FAILURES

### SY-001 — Screener-generated `proposal_id`s collide across separate scan runs
- **Severity:** CRITICAL
- **Component:** `src/workflows/candidate_generation.py:206-211` (`_next_id`); `src/workflows/morning_scan.py:134-137`; `src/orchestration/pipeline.py:387-394`
- **Description:** `generate_candidates()` builds each `TradeProposal.proposal_id` as `f"{prefix}-{ticker}-{counter}"`, where `counter` is a *local variable restarting at 0 on every call*. `morning_scan.py` never overrides the prefix (always `"scan"`). The id therefore carries no date, timestamp, expiration, or strike information — it depends entirely on call order within one invocation. `run_order_pipeline` pins `client_order_id = proposal.proposal_id` specifically because the pipeline's own docstring calls it "the proposal's own *stable* id," relied on by `PaperBroker`'s idempotency store to recognize a retried call as a duplicate.
- **Possible consequence:** if a `PaperBroker`/idempotency-store instance is reused across more than one `/morning-scan` invocation (a very plausible shape for a persistent multi-day research/paper-trading loop — nothing in `PaperBroker.place_order` prevents this), the *same* ticker's first successful candidate on two different days both get `proposal_id = "scan-{TICKER}-1"`. The second day's `place_order` call finds an existing order under that id and returns the **first day's stale result** without validating collateral or attempting to fill the genuinely different trade — a real new order is silently dropped and misreported as an old fill.
- **Reproduction:** run `generate_candidates` twice against a shared `PaperBroker` instance for the same ticker (different chain data each time, as would happen on two different days) — both first-successful candidates receive `proposal_id="scan-{TICKER}-1"`; the second `place_order` call returns the first call's `Order` object unchanged.
- **Recommended remediation (not applied):** derive `proposal_id` from something that's actually unique per intended trade — e.g. include the scan date, the selected expiration, and the strike(s) in the id, not just an in-call counter.
- **Status: FIXED (Step 17B).** `_next_id` in `src.workflows.candidate_generation.generate_candidates` now builds `proposal_id` from `{prefix}-{ticker}-{scan date}-{strategy tag}-{expiration}-{strike(s)}-{counter}`, unique per intended trade rather than per in-call counter alone. Regression tests: `tests/unit/workflows/test_candidate_generation.py::TestProposalIdsAreUniqueAcrossScanRunsRegressionSY001` (proves the same ticker/strike on two different scan dates no longer collides, and that the id encodes the scan date).

### SY-002 — Idempotency protection does not survive a process restart
- **Severity:** CRITICAL
- **Component:** `src/brokers/base.py:192-206` (`InMemoryIdempotencyStore`); `src/orchestration/pipeline.py:246-257`; `src/brokers/paper.py:254-277`
- **Description:** `InMemoryIdempotencyStore` is a plain in-process dict, explicitly documented as "process-local only — lost on restart." `Broker.place_order`'s own docstring states the entire retry-safety model depends on a caller re-calling with the *same* `client_order_id` after a failure/timeout — a guarantee that only holds if the store recognizing that id survives between the original attempt and the retry. It doesn't: `PaperBroker`, `InMemoryDatabase`, and every other in-memory store are all reconstructed fresh on process restart.
- **Possible consequence:** if `PaperBroker.place_order` succeeds and fills, but the process crashes before the caller records/acts on the result, a retry (by a human or scheduler) in a new process finds no matching idempotency entry and places a brand-new order — indistinguishable from the first (lost) one. If a human interprets a regenerated Fidelity ticket as "the retry of the one that failed" and manually enters it while the original had also gone through, this duplicates a *real* order.
- **Reproduction:** run `run_order_pipeline` to a successful fill, discard the process/objects (simulating a crash), reconstruct fresh `PaperBroker`/`InMemoryIdempotencyStore`/`InMemoryDatabase` instances, and re-run the identical `PipelineRequest` — it fills again as if new, with no duplicate detection.
- **Recommended remediation (not applied):** this requires the persisted database and idempotency store already tracked as a standing Phase 0 item in `progress.md` — no in-memory-only workaround can close this gap; flagging here as a confirmed, present-tense risk rather than a hypothetical one.
- **Status: FIXED (Step 17B).** Added genuinely durable (not in-memory) implementations of both interfaces named in this finding: `src.brokers.base.SqliteIdempotencyStore` and `src.orchestration.pipeline.SqliteDatabase`, each backed by a single sqlite file, each operation opening and closing its own connection so the store itself holds no in-process state a crash could lose. `InMemoryIdempotencyStore`/`InMemoryDatabase` remain the defaults (unchanged) for tests and short-lived runs; the durable classes are drop-in implementations of the same `IdempotencyStore`/`Database` interfaces for any caller that needs crash-survival. This does not replace the still-open, larger Phase 0 persisted-order-state-machine item, but it closes the specific "cannot survive a process restart" gap this finding named. Regression tests: `tests/unit/brokers/test_base.py::TestSqliteIdempotencyStoreRegressionSY002` and `tests/unit/brokers/test_paper_broker.py::TestIdempotencySurvivesRestartRegressionSY002` (a `PaperBroker` wired to a sqlite-backed store recognizes a retry after the Python objects are discarded and a fresh instance is constructed against the same file — and a contrasting test shows the in-memory default does not), plus `tests/unit/orchestration/test_pipeline.py::TestSqliteDatabaseSurvivesRestartRegressionSY002`.

### SY-003 — An exception in the Portfolio-update stage causes a permanent partial write
- **Severity:** HIGH
- **Component:** `src/orchestration/pipeline.py:443-453`
- **Description:** Every other pipeline stage (Quant, Devil's Advocate, Portfolio Manager, Risk Engine, Order Validator, PaperBroker) is wrapped in `try/except Exception`, and on failure still calls `_record(...)` so a `DatabaseRecord` is always written. Stage 7 (`stages.portfolio_update_stage(...)`, an injectable `Callable`) is not wrapped — an exception here propagates out of `run_order_pipeline` uncaught, and `_record()` (the only path that writes to the database) is never reached.
- **Possible consequence:** at the point of failure, `PaperBroker.place_order` has already completed — the order is filled, cash debited, `Fill` records appended — but the database permanently has no record of it. If the caller retries the same proposal, `PaperBroker.place_order` short-circuits on the existing idempotency entry and returns the already-filled order *without re-running the failing update logic*, meaning the database gap can be permanent, not just delayed. Nothing reconciles this afterward (`PaperBroker.reconcile()` trivially reports clean, since a paper broker's local state is definitionally "the broker's state").
- **Reproduction:** supply a `portfolio_update_stage` callable that raises on any input, run the pipeline to a successful `PaperBroker` fill, and observe: the returned exception propagates out of `run_order_pipeline`, and `database.all()` never gains a record for that filled order, including on a subsequent retry with the same proposal.
- **Recommended remediation (not applied):** wrap stage 7 in the same `try/except Exception -> _record(...)` pattern every other stage already uses, distinguishing "filled but portfolio-update failed" as its own recorded outcome rather than an unhandled crash.
- **Status: FIXED (Step 17B).** Stage 7 is now wrapped in the same `try/except Exception` pattern as every other stage. On failure, `_record(...)` still runs (so the real fill is never silently lost from the database), and the outcome preserves the true fill status (`FILLED`/`PARTIALLY_FILLED`, never mislabeled as a rejection) with `rejected_stage="portfolio_update"` and a message naming the failure — a distinct, always-persisted "filled but portfolio update failed" outcome rather than an unhandled crash. Regression tests: `tests/unit/orchestration/test_pipeline.py::TestPortfolioUpdateFailureRegressionSY003` (proves the pipeline call never raises, the real fill is still reported, and a database record is always written despite the failure).

### SY-004 — Sequential candidates in one scan run share a stale portfolio snapshot
- **Severity:** CRITICAL
- **Component:** `src/workflows/morning_scan.py:150-168`; `src/orchestration/pipeline.py:444`; `src/risk/engine.py:255-287`
- **Description:** `run_morning_scan`'s loop builds a `PipelineRequest` with `portfolio=inputs.portfolio` — the *same* object — for every candidate in the batch. `run_order_pipeline` does return an `updated_portfolio` in its outcome when a fill occurs, but `run_morning_scan` never feeds it back in before evaluating the next candidate. Every Risk Engine check that depends on current cash/nav/positions — buying power, cash reserve %, capital-deployed cap, duplicate-position, concentration, correlation, stress test — is therefore evaluated against the *pre-scan* snapshot for every candidate, never against what the same run has already approved moments earlier.
- **Possible consequence:** a scan that generates multiple approvable candidates across different tickers can approve each one *individually* against limits that, applied to the batch cumulatively, would have rejected some of them — e.g. two CSPs that would each individually pass the 20% minimum-cash-reserve check against the untouched starting portfolio, but that together deploy far more capital than the reserve rule intends. This is a real, live, currently-wired defeat of the Risk Engine's own stated portfolio-level protections, reachable on any `/morning-scan` run that approves more than one candidate — and every approved candidate can independently produce a human-facing `AWAITING_HUMAN` Fidelity ticket, meaning a human could manually execute several "APPROVE"-labeled real trades whose *combined* effect was never actually checked against the platform's own risk limits.
- **Reproduction:** construct a `Portfolio` near a cash-reserve or concentration boundary, generate two candidates on different tickers each individually just inside the limit, and run them through `run_morning_scan` — both are evaluated against the identical starting `Portfolio` and can both be approved, even though applying the first's economics before evaluating the second would have pushed the second past the limit.
- **Recommended remediation (not applied):** thread `outcome.updated_portfolio` forward as the `portfolio` for the next candidate's `PipelineRequest` within the same scan run (falling back to `inputs.portfolio` only for the first candidate, or whenever the previous candidate did not fill), so cumulative effects within one run are actually seen by the Risk Engine's own checks.
- **Status: FIXED (Step 17B).** `run_morning_scan` now tracks a `current_portfolio` that starts at `inputs.portfolio` and is replaced by each candidate's own `outcome.updated_portfolio` whenever a fill occurs (left unchanged for a candidate that didn't fill), threaded into every subsequent candidate's `PipelineRequest`. Regression tests: `tests/unit/workflows/test_morning_scan.py::TestPortfolioThreadedAcrossCandidatesRegressionSY004` (patches `run_order_pipeline` directly to prove the second candidate's request carries the first candidate's own fill result, not the stale pre-scan snapshot, and that a no-fill candidate doesn't reset the carried portfolio). Paired with the SY-005 fix (`Portfolio.cash` now actually changes on a fill), this closes the full cumulative-enforcement gap this finding described.

### SY-005 — `Portfolio.cash` is never updated after a fill
- **Severity:** HIGH
- **Component:** `src/orchestration/pipeline.py:156-183` (`default_portfolio_update_stage`)
- **Description:** `default_portfolio_update_stage` returns `portfolio.model_copy(update={"positions": [*portfolio.positions, new_position]})` — the *only* field ever updated is `positions`. `cash` (and therefore `capital_deployed_pct`/`cash_reserve_pct` computed from it) never reflects the credit received or collateral committed by the fill that was just recorded. This is a distinct, compounding root cause alongside SY-004: even a single caller processing trades one at a time, correctly threading `updated_portfolio` forward, would still see a `Portfolio` whose `cash` field drifts further from reality after every fill.
- **Possible consequence:** every cash-dependent Risk Engine check (buying power, minimum cash reserve, capital-deployed cap) is evaluated against a number that never actually reflects prior fills — the exact accounting corruption the audit's "corrupt accounting" category asks about.
- **Reproduction:** call `default_portfolio_update_stage(portfolio, proposal, qa, order)` for a filled order and inspect the result — `.cash` is bit-for-bit identical to the input `portfolio.cash`, regardless of the credit received or collateral required by the new position.
- **Recommended remediation (not applied):** update `cash` in the same `model_copy` call, using the fill's own proven economics (the credit/debit actually realized and any collateral now committed), the same way `PaperBroker`'s own internal `_cash` is correctly updated on every fill.
- **Status: FIXED (Step 17B).** `default_portfolio_update_stage` now reduces `cash` by the new position's own `capital_at_risk` in the same `model_copy` call — consistent with this `Portfolio` type's own semantics (`capital_deployed_pct` is defined as exactly `(nav - cash) / nav`, so `cash` means "NAV not committed to an open position," not a raw brokerage cash balance). Since `model_copy` does not re-run Portfolio's own `Field(ge=0)` validator, an explicit check raises a clear `ValueError` if this would drive cash negative, rather than silently producing an invalid Portfolio — paired with the SY-003 fix, this is caught and recorded as a distinct portfolio_update failure instead of corrupting downstream state. Regression tests: `tests/unit/orchestration/test_portfolio_update_stage.py` (5 tests: cash decreases by capital_at_risk, partial fills scale correctly, NAV is unchanged by opening a position, and the negative-cash case fails closed).

### SY-006 — Reconciliation discrepancies are only ever reported, never acted on
- **Severity:** HIGH
- **Component:** `src/workflows/reconciliation.py:48-90`; `src/workflows/morning_scan.py:121,176`
- **Description:** `reconcile_portfolio` is a pure function with no write path anywhere — it returns a `ReconciliationResult` that `run_morning_scan` only threads into the final human-facing report, never consults before generating new candidates or before the Risk Engine's own duplicate-position check runs (which uses only `request.portfolio.positions`, the same stale snapshot, never the confirmed-Fidelity-position data or the reconciliation result itself).
- **Possible consequence:** if a human has already manually placed a real Fidelity trade that the internal `Portfolio` was never updated to reflect, `reconcile_portfolio` correctly flags it as `missing_from_internal` — but the *same scan run* will still happily generate a new candidate for that same ticker, and the duplicate-position check (blind to the confirmed-but-untracked position) can approve a second, real-money duplicate trade recommendation in the very report that flagged the discrepancy.
- **Reproduction:** supply a `ConfirmedFidelityPosition` for a ticker with no matching internal `Portfolio` position, and a universe/chain that would otherwise generate a new candidate for that same ticker — `run_morning_scan` reports the discrepancy *and* still produces a fresh candidate (and potentially an approved ticket) for the same underlying/strategy/expiration.
- **Recommended remediation (not applied):** either exclude a ticker from new-candidate generation whenever reconciliation flags an unresolved `missing_from_internal` discrepancy for it, or feed confirmed Fidelity positions into the duplicate-position check directly, not just the internal `Portfolio`.
- **Status: FIXED (Step 17B).** Implemented the first option: `run_morning_scan` now computes the set of tickers with an unresolved `missing_from_internal` discrepancy before generating any candidates, and skips those tickers entirely for the rest of that scan run (recorded in a new `reconciliation_screened_out` report field and rendered in the report text), rather than only reporting the discrepancy after the fact. Regression tests: `tests/unit/workflows/test_morning_scan.py::TestReconciliationBlocksNewCandidatesRegressionSY006` (proves a ticker with an untracked Fidelity position gets no new candidate even though the same universe/chain would otherwise produce an approved one, that the skip is rendered, and that it's scoped per-ticker, not a blanket halt).

### SY-007 — Order Validator's own duplicate-id check is dead code
- **Severity:** MEDIUM
- **Component:** `src/brokers/order_validator.py:56,87-89`; `src/orchestration/pipeline.py:387-394`
- **Description:** `validate_and_build_order_request` accepts `existing_client_order_ids: frozenset[str] = frozenset()` and rejects an id already present — but its only production caller (`run_order_pipeline`) never supplies this argument, so it is always checked against an empty set and can never fire.
- **Possible consequence:** not independently exploitable today (`PaperBroker`'s own idempotency store still catches the same-process/same-store duplicate case) — but this is a piece of apparent duplicate protection that gives false confidence to a reviewer skimming this file, who would reasonably believe it's an active safeguard.
- **Reproduction:** `grep -rn "existing_client_order_ids" src/` shows the parameter is declared and checked in `order_validator.py` but never passed a non-empty value anywhere in `src/`.
- **Recommended remediation (not applied):** either wire a real set of already-submitted ids into this call, or remove the parameter to avoid the false impression of an active check.

### SY-008 — Execution-quality time-of-day bucketing assumes an unenforced timezone
- **Severity:** MEDIUM
- **Component:** `src/workflows/execution_quality.py:35-49`
- **Description:** `_time_of_day_bucket` buckets purely by `dt.hour`, with a comment noting the caller is "responsible for supplying a timestamp in whatever single timezone this platform's reporting standardizes on." `ExecutionConfirmation.confirmed_at` only enforces timezone-*awareness*, not a specific zone — and every other system-generated timestamp elsewhere in this codebase uses UTC (the platform's own de facto convention). `confirmed_at` is human-entered data with nothing normalizing what zone a human actually enters it in.
- **Possible consequence:** if `confirmed_at` is recorded in UTC (consistent with the rest of the platform), a 9:35am ET market-open fill (~13:35-14:35 UTC depending on DST) would be bucketed as "afternoon," and a noon ET fill (~16:00-17:00 UTC) as "after_hours" — the weekly Fidelity execution-quality review's `by_time_of_day` breakdown would be systematically mislabeled with no error raised anywhere.
- **Reproduction:** build a `FidelitySlippageRecord` from an `ExecutionConfirmation.confirmed_at` set to a UTC time equivalent to 9:35am ET (e.g. 13:35 or 14:35 UTC) — `_time_of_day_bucket` returns `"afternoon"`, not `"morning"`.
- **Recommended remediation (not applied):** require and document a specific timezone convention for `confirmed_at` (or convert explicitly to market-local time before bucketing) rather than leaving it an unenforced assumption.

### SY-009 — No market-holiday calendar exists anywhere
- **Severity:** LOW
- **Component:** `src/backtest/engine.py`, `src/backtest/walk_forward.py`, `src/workflows/candidate_generation.py`
- **Description:** DTE is computed as raw calendar-day subtraction throughout (standard, correct options-industry convention for DTE itself) — but `trading_days`/scheduling inputs to the backtest engine are opaque, externally-supplied lists validated only for sort order; no market-calendar module (NYSE holidays, etc.) exists anywhere in the repository.
- **Possible consequence:** any future caller wiring the backtest engine for real is one naive weekday-only date range away from silently treating exchange holidays (Thanksgiving, Christmas, July 4th, etc.) as trading days, with nothing in this codebase to catch it.
- **Reproduction:** N/A — no production caller currently supplies a real `trading_days` list (only tests do); this is a documented absence, not a triggerable defect in wired code.
- **Recommended remediation (not applied):** when a real scheduling/backtest caller is built, use a real market-calendar library (e.g. `pandas_market_calendars`) rather than a naive weekday range.

### SY-010 — No locking anywhere; current race-freedom is incidental
- **Severity:** LOW
- **Component:** `src/brokers/paper.py:342-408`; `src/brokers/base.py:192-206`; `src/orchestration/pipeline.py`; `src/llm/client.py:113-115`
- **Description:** No `Lock`/`Semaphore` exists anywhere in `src/`. `PaperBroker`'s check-then-act idempotency sequences have no `await` between the check and the write, and every LLM call in this pipeline uses the *synchronous* Anthropic client (not `AsyncAnthropic`) — so within a single asyncio event loop, nothing here can actually interleave today; these sequences are atomic only because no genuine suspension point exists between them.
- **Possible consequence:** this safety is incidental to the current all-synchronous, no-real-network-I/O implementation, not a designed guarantee. It would silently stop holding the moment any stage becomes genuinely concurrent (e.g. swapping in `AsyncAnthropic` for real network calls — the natural evolution path for this codebase) or is invoked from more than one OS thread (e.g. a future web server using a thread-pool executor).
- **Reproduction:** N/A — not reproducible under the current codebase's exclusively-synchronous execution model; flagged as a latent structural gap for when that changes.
- **Recommended remediation (not applied):** add explicit locking around the idempotency check-then-act sequences before introducing any genuine concurrency (real async I/O or multi-threaded invocation).

---

## LLM FAILURES

### LM-001 — Devil's Advocate narrative text is re-embedded, unsanitized, into the Portfolio Manager's next LLM call
- **Severity:** MEDIUM
- **Component:** `src/orchestration/pipeline.py:143-153` (`_adversarial_review_from`); `src/llm/portfolio_manager.py` (`build_portfolio_manager_context`)
- **Description:** `DevilsAdvocateReview.why_not_thesis` is reused as `AdversarialReview.critique`, which is then JSON-serialized into the Portfolio Manager's own `user_content` for its subsequent LLM call — unsanitized, undelimited, not labeled as untrusted narrative. This is a genuine mechanical prompt-injection vector: text an earlier LLM call produced becomes part of a later LLM call's input.
- **Possible consequence:** a compromised or adversarially-steered Devil's Advocate output could embed injected instructions inside `why_not_thesis` that attempt to manipulate the Portfolio Manager's subsequent reasoning. The blast radius is structurally bounded today: the pipeline never reaches the Portfolio Manager at all if Devil's Advocate returns REJECT/REPRICE_REQUIRED; `PortfolioDecision` has zero numeric fields and zero execution authority (the worst outcome is biasing an advisory `propose_advance` vs. `reject`/`hold_cash` recommendation, which still must clear the fully independent Risk Engine); and a proposal/regime cross-check would catch some injected-output failure shapes. But the mechanism itself is real and unmitigated.
- **Reproduction:** trace the data flow — `why_not_thesis` (free text, up to 2000 chars, entirely LLM-authored) flows through `_adversarial_review_from` into `PortfolioManagerInputs.devil_advocate_review.critique`, then into the JSON payload passed as `user_content` to the Portfolio Manager's `complete_structured` call, with no sanitization, delimiting, or "this is untrusted narrative" framing anywhere in between.
- **Recommended remediation (not applied):** delimit/label untrusted narrative fields distinctly when reinjecting them into a later prompt (e.g. explicit framing in the system prompt that `devil_advocate_review.critique` is untrusted narrative content, not instruction) as defense-in-depth — even though no current exploit path reaches capital, this would matter more if a future change gives Portfolio Manager more authority.

### LM-002 — "No numeric field" test coverage exists for only 3 of 15 non-`TradeProposal` LLM schemas
- **Severity:** MEDIUM
- **Component:** `tests/unit/llm/test_portfolio_decision_schema.py`, `test_devils_advocate_review_schema.py`, `test_strategy_research_review_schema.py`
- **Description:** Each of these three test files iterates its one schema's `model_fields` and asserts no field has a numeric annotation — a real, working guard. But it exists for only `PortfolioDecision`, `DevilsAdvocateReview`, and `StrategyResearchReview`. `DevilsAdvocateReview`'s test additionally checks one nested submodel (`FailureScenario`) but not its other two (`RiskCategoryAssessment`, `FidelityExecutionRiskAssessment`). No such guard exists at all for `AdversarialReview`, `RiskReviewNote`, `TradeManagerOutput`, `PerformanceAuditReport`, `MarketRegimeAssessment`, `OpportunityScan`, `CandidateHighlight`, `RiskFlag`, `PortfolioManagerReview`, or `StrategyAnalystOutput`. No generic/parametrized test iterates all `_StrictModel` subclasses automatically, so protecting a *new* schema requires someone to remember to write a matching test — nothing fails automatically as a safety net.
- **Possible consequence:** today, per a full field-by-field inventory performed as part of this audit, no unintended numeric field actually exists on any of the 10 uncovered schemas or 2 uncovered nested submodels — but this codebase's own docstrings claim "no numeric field anywhere" for several of these schemas with zero automated verification of that claim, and a future change could add one silently.
- **Reproduction:** add a `float` field to, e.g., `RiskReviewNote` — every existing test in `tests/unit/llm/` still passes.
- **Recommended remediation (not applied):** add a single parametrized test that walks every `_StrictModel` subclass in `src.llm.schemas` (via `__subclasses__()` or an explicit registry) and asserts no numeric field exists on any of them, replacing the current per-schema hand-written approach with one that automatically covers new schemas.

**Note:** the audit's two central LLM-boundary questions — *"can an LLM-requested contract count ever become the approved size?"* and *"can a hallucinated numeric value ever reach a real calculation?"* — were both traced end to end and found sound: `cap_requested_contracts`'s `min(requested, max_allowed)` is structurally incapable of returning more than the deterministic cap in any code path, including every exception-handler fallback; and no construction site anywhere re-inserts LLM output into a `QuantitativeAnalysisContext`/`PortfolioStateContext`/`MarketSnapshotContext` treated as ground truth. See "Verified safe" below.

### LM-003 — `profit_target`/`management_dte` reach the Fidelity ticket unchecked beyond schema bounds (currently dormant)
- **Severity:** LOW (would become MEDIUM if the noted path is wired live)
- **Component:** `src/risk/engine.py:412-439` (`_build_approved_order`)
- **Description:** `proposal.profit_target` (schema-bounded `0 < x <= 1`) and `proposal.management_dte` (schema-bounded `0 <= x <= 365`, and validated `<=` total DTE) flow directly into `ApprovedOrder`/`FidelityTradeTicket` fields rendered to the human — unlike price/size fields, neither is independently recomputed or policy-capped by Python the way `contracts_requested` is.
- **Possible consequence:** a hallucinated `profit_target=0.99` or `management_dte=364` (both within schema bounds) would reach the human-facing ticket as a *suggested* exit rule. Consequence is bounded today: this only shapes a suggestion on a MANUAL, human-reviewed ticket, and in the actually-wired `/morning-scan` path these values are always Python/config-derived (`QuantFilterConfig`), never LLM-authored — the Strategy Analyst LLM persona that could set them is defined but not wired into the order pipeline today.
- **Reproduction:** construct a `TradeProposal` with `profit_target=0.99` and trace it through `evaluate_trade_proposal` — `_build_approved_order` uses it verbatim with no independent recomputation or tightening.
- **Recommended remediation (not applied):** if/when an LLM-authored `TradeProposal` path is wired directly into the order pipeline, add a policy cap on these two fields analogous to how contract count is capped, rather than trusting the schema's own range bounds alone.

### LM-004 — `PortfolioManagerReview` is unused, untested dead code
- **Severity:** LOW (housekeeping)
- **Component:** `src/llm/schemas.py:456-463`
- **Description:** Defined, structurally near-identical in purpose to `PortfolioDecision`, but never constructed or referenced anywhere in `src/` outside its own definition.
- **Possible consequence:** none today — but it has no numeric-field test and no usage, so it's a liability if it were ever wired in later without the same scrutiny `PortfolioDecision` received.
- **Reproduction:** `grep -rn "PortfolioManagerReview" src/` shows only the definition site.
- **Recommended remediation (not applied):** remove it if genuinely superseded, or give it the same test coverage as `PortfolioDecision` if it's still intended for future use.

---

## FIDELITY SECURITY AUDIT

### FS-001 — No Fidelity credentials, cookies, or unofficial access anywhere in the repository (verified safe)
- **Severity:** informational (verified safe)
- **Component:** repository-wide
- **Description:** A repository-wide, case-insensitive search for `password`, `username`, `MFA`, `cookie`, `session_token`, `bearer`, browser-automation libraries (`selenium`, `playwright`, `puppeteer`, `webdriver`), and Fidelity-specific network access (`fidelity.com`, `api.fidelity`, `requests.get/post`, `urllib`, `aiohttp`) found **zero** actual credentials, cookies, tokens, or automation code. Every match was either (a) prose/docstrings explicitly describing the intentional *absence* of these things, (b) a test asserting the absence (`tests/unit/brokers/test_ibkr_config.py`'s `forbidden_substrings` list, itself a positive control), or (c) unrelated (`test_ibkr_config.py` concerns IBKR, a different, already-paper-only broker). No `.env`, credential, or secret files exist in the repository.
- **Reproduction:** `grep -rniE "password|username|mfa|cookie|session_token|bearer|selenium|playwright|puppeteer|webdriver" .` and `find . -iname "*.env*" -o -iname "*credential*" -o -iname "*secret*"` — both confirm the above.

### FS-002 — `FidelityManualProvider` cannot submit any order (verified safe)
- **Severity:** informational (verified safe)
- **Component:** `src/brokers/fidelity.py:365-412`
- **Description:** `FidelityManualProvider` has exactly one method, `generate_trade_ticket`, which returns a data object (`FidelityTradeTicket`) — it does not place, submit, or send anything. The entire file imports only `uuid`, `datetime`, `enum`, `typing`, `pydantic`, `src.data.option_chain`, and `src.data.provider` — no HTTP client, no network library, no browser-automation import of any kind.
- **Reproduction:** `grep -n "def \|import " src/brokers/fidelity.py` — confirms the complete method/import list; no `place_order`/`submit_order`/`send_order` method exists anywhere in the file.

### FS-003 — `execution_mode` has no runtime override mechanism (verified safe)
- **Severity:** informational (verified safe)
- **Component:** `config/brokers.yaml`; `src/risk/broker_constraints.py`
- **Description:** Unlike `config/risk_limits.yaml`'s numeric policy values (which support an optional environment-variable override for ops-time changes, e.g. `OPTIONS_AGENT_RISK_...`), `config/brokers.yaml`'s `execution_mode` field has **no** environment-variable override mechanism anywhere in `broker_constraints.py`'s loader — confirmed by grep (`_env`/`environ`/`getenv` have zero matches in that file). Fidelity's `execution_mode: MANUAL` can only be changed by directly editing and committing a change to the YAML file itself.
- **Reproduction:** `grep -n "_env\|environ\|getenv" src/risk/broker_constraints.py` — no matches.

### FS-004 — No LLM-facing schema, context object, or agent tool can influence broker capabilities or execution mode (verified safe)
- **Severity:** informational (verified safe)
- **Component:** `src/llm/*.py`; `.claude/agents/*.md`
- **Description:** A repository-wide search of every file in `src/llm/` found zero references to `broker_capabilities`/`BrokerCapabilities` — no LLM schema, context dataclass, or orchestration function in that package has any field, parameter, or import related to broker execution mode. Separately, every tool listed in every agent persona's frontmatter (`devil_advocate.md`, `market_regime.md`, `opportunity_scanner.md`, `performance_auditor.md`, `portfolio_manager.md`, `risk_reviewer.md`, `strategy_analyst.md`, `strategy_research.md`, `trade_manager.md`) is a read-only `get_*` tool — `get_screened_candidates`, `get_portfolio_risk`, `get_market_context`, `get_agent_outputs`, `get_risk_limits_summary`, `get_backtest_summary`, `get_open_positions`. No agent has a write-, configure-, or execute-shaped tool of any kind. **An LLM has no data path, and no tool, through which it could attempt to change `MANUAL_EXECUTION` to automatic — via prompt injection, configuration change, agent instruction, or tool call.**
- **Reproduction:** `grep -n "broker_capabilities\|BrokerCapabilities" src/llm/*.py` (zero matches); `grep -A 15 "^tools:" .claude/agents/*.md | grep "  - "` (every result is a `get_*` name).

### FS-005 — `FidelityTradeTicket` can in principle be constructed directly at a terminal status, bypassing the lifecycle functions
- **Severity:** LOW (no live exploit path)
- **Status: FIXED (Step 22)**
- **Component:** `src/brokers/fidelity.py:262-320`
- **Description:** `transition()` and `confirm_fill()` correctly enforce the full state-transition graph and reject any attempt to reach FILLED/PARTIALLY_FILLED except through `confirm_fill()` with a real `ExecutionConfirmation`, from `ORDER_ENTERED`/`PARTIALLY_FILLED` only. However, these functions operate on an *existing* ticket object — nothing prevents a caller from constructing a brand-new `FidelityTradeTicket(status=TicketStatus.FILLED, execution_confirmation=ExecutionConfirmation(...), ...)` directly via the Pydantic constructor, skipping the entire lifecycle. The model's own validator (`_execution_confirmation_only_when_filled`) only checks internal consistency (status matches presence of a confirmation object), not that the object was actually produced via the correct sequence of calls.
- **Possible consequence:** none currently live — a repository-wide search confirms `FidelityTradeTicket(` is constructed in exactly one place in `src/`, inside `generate_trade_ticket`, always hardcoded to `status=TicketStatus.AWAITING_HUMAN`. This is a defense-in-depth gap (the type itself doesn't enforce provenance), not an active vulnerability.
- **Reproduction (pre-fix):** `FidelityTradeTicket(status=TicketStatus.FILLED, execution_confirmation=ExecutionConfirmation(confirmed_by="anyone", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=1.0, confirmed_at=<now>), ...all other required fields...)` constructed successfully without ever having passed through `AWAITING_HUMAN` → `ORDER_ENTERED` → `confirm_fill()`.
- **Fix (Step 22):** a new `@model_validator(mode="after")` on `FidelityTradeTicket` (`src/brokers/fidelity.py`) raises `ValueError` unless `status == TicketStatus.AWAITING_HUMAN` at construction time. This closes the gap without touching the legitimate state machine: pydantic v2's `model_copy(update=...)` — which `transition()`/`confirm_fill()` exclusively use to move a ticket through its lifecycle — never re-runs `@model_validator` hooks (verified directly), so every real transition is unaffected; only a caller building a brand-new instance at a non-initial status now fails closed. The one real production construction site, `FidelityManualProvider.generate_trade_ticket`, already always constructs at `AWAITING_HUMAN`, so the fix is a no-op there.
- **Regression tests:** `tests/unit/brokers/test_fidelity_schemas.py` (the pre-existing tests that exercised non-`AWAITING_HUMAN` ticket states via direct construction were updated to reach those states via `.model_copy(update=...)` instead, matching how `transition()`/`confirm_fill()` actually work); `tests/acceptance/test_fidelity_manual_only.py::TestFS005SoleTicketConstructionSiteIsHardcodedToAwaitingHuman` (verifies exactly one `FidelityTradeTicket(` construction call site exists in `src/`, and it is `AWAITING_HUMAN`). Full suite re-verified passing after the fix (2388 tests, 4 skipped, 0 failed).

---

## TRADE STATE AUDIT

### TS-001 — `RISK_APPROVED` cannot reach `FILLED` without explicit execution confirmation (verified safe)
- **Severity:** informational (verified safe)
- **Component:** `src/brokers/fidelity.py:100-125` (`_ALLOWED_TRANSITIONS`), `:327-362` (`transition`/`confirm_fill`)
- **Description:** `RISK_APPROVED`'s only allowed transitions are to `AWAITING_HUMAN`, `REJECTED`, or `EXPIRED` — `FILLED`/`PARTIALLY_FILLED` do not appear in *any* value-set in the entire `_ALLOWED_TRANSITIONS` graph. `transition()` explicitly raises `InvalidTransitionError` if asked to reach either fill status. The *only* function that can produce a FILLED/PARTIALLY_FILLED ticket is `confirm_fill()`, and only from `ORDER_ENTERED` or `PARTIALLY_FILLED`, and only given a real `ExecutionConfirmation` object (`confirmed_by`, `confirmation_source`, `filled_quantity`, `fill_price`, `confirmed_at` — all required, non-optional fields).
- **Reproduction:** attempting `transition(ticket_at_RISK_APPROVED, TicketStatus.FILLED, at=...)` raises `InvalidTransitionError` before even consulting the transition graph (fill statuses are rejected by `transition()` unconditionally, by name, before the graph lookup).

### TS-002 — PaperBroker fills and Fidelity real fills are structurally disjoint types with no conversion path (verified safe)
- **Severity:** informational (verified safe)
- **Component:** `src/brokers/base.py` (`Order`, `Fill`) vs. `src/brokers/fidelity.py` (`FidelityTradeTicket`, `ExecutionConfirmation`)
- **Description:** These are two entirely separate class hierarchies. A repository-wide search confirms `confirm_fill(` and `ExecutionConfirmation(` are referenced only inside `fidelity.py` itself — no production code anywhere constructs an `ExecutionConfirmation` from a `PaperBroker` `Order`/`Fill`, and no function converts between the two type families in either direction.
- **Reproduction:** `grep -n "confirm_fill(\|ExecutionConfirmation(" src/` — matches only within `fidelity.py`'s own definitions and docstrings.

### TS-003 — See FS-005
- Direct-construction lifecycle bypass is a defense-in-depth gap in the type itself, not a live exploit — documented once, under Fidelity Security, to avoid duplication.

### TS-004 — CLOSE/ROLL proposals crash the Order Validator with an uncaught exception
- **Severity:** HIGH
- **Component:** `src/risk/engine.py:340-345` (`approved_order` only built for `TradeAction.OPEN`); `src/brokers/order_validator.py:72` (`approved_order.quantity` accessed with no `None` check)
- **Description:** `evaluate_trade_proposal` only constructs an `ApprovedOrder`/`FidelityTradeTicket` when `proposal.action == TradeAction.OPEN`. For `TradeAction.CLOSE` or `TradeAction.ROLL` (both valid, schema-supported values — the Trade Manager persona is explicitly documented as using them), a proposal that clears every other risk check still receives `decision=APPROVE`/`RESIZE` with `approved_order=None`. `validate_and_build_order_request`'s first parameter is typed `approved_order: ApprovedOrder` (no `| None`), and its very first attribute access, `approved_order.quantity`, is unguarded — passing `None` raises a plain `AttributeError`, not the module's own `OrderValidationError`. `run_order_pipeline`'s Order Validator stage only catches `except OrderValidationError`, so this `AttributeError` propagates all the way out of `run_order_pipeline` uncaught, crashing the entire pipeline call.
- **Possible consequence:** any legitimate close-or-roll trade recommendation that the Risk Engine would otherwise approve crashes the pipeline instead of producing a clean result (approved order, rejection, or otherwise) — the CLOSE/ROLL trade-action pathway is completely broken end-to-end at the pipeline level, not merely a narrow edge case, since managing (closing/rolling) existing positions is a core, named platform capability.
- **Reproduction:** build a `TradeProposal` with `action=TradeAction.CLOSE` that would otherwise pass every Risk Engine check (fresh data, liquid contracts, sufficient cash, no concentration/stress violations), call `evaluate_trade_proposal(...)` (returns `approved_order=None`, `decision=APPROVE`), then call `validate_and_build_order_request(None, risk_decision=RiskDecision.APPROVE, approved_contracts=<N>, broker_capabilities=<caps>, client_order_id="x")` — raises `AttributeError: 'NoneType' object has no attribute 'quantity'`. Confirmed via source inspection that no test anywhere in `tests/` exercises `TradeAction.CLOSE`/`ROLL` through the Risk Engine or Order Validator (only a schema-level test touches the enum values at all).
- **Recommended remediation (not applied):** either (a) build an `ApprovedOrder` for CLOSE/ROLL actions too (using the appropriate closing leg actions), or (b) have `evaluate_trade_proposal` explicitly reject CLOSE/ROLL proposals with a clean `ReasonCode` if that capability isn't actually implemented yet, and (c) regardless, add a `None` check at the top of `validate_and_build_order_request` that raises `OrderValidationError` (not an `AttributeError`) for a missing `approved_order`, so this class of gap fails clean rather than crashing even after (a)/(b) are addressed.
- **Status: FIXED (Step 17B).** Implemented both (b) and (c). `_evaluate` now rejects any `proposal.action != TradeAction.OPEN` immediately after the kill-switch check with a new `ReasonCode.REJECT_UNSUPPORTED_ACTION`, before any approved-order construction is attempted — CLOSE/ROLL still isn't an implemented capability, but a proposal requesting it now always gets a normal `RiskDecisionResult`, never a crash. `validate_and_build_order_request`'s `approved_order` parameter is now typed `ApprovedOrder | None`, with an explicit `None` check at the top raising `OrderValidationError` — defense-in-depth per the audit's own recommendation, independent of (b). Regression tests: `tests/unit/risk/test_engine_bypass_attempts.py::TestCloseRollProposalsRegressionTS004`, `tests/unit/brokers/test_order_validator.py::TestMissingApprovedOrderRegressionTS004`, and an end-to-end `tests/unit/orchestration/test_pipeline.py::TestCloseRollProposalsRegressionTS004` proving a CLOSE/ROLL proposal no longer crashes `run_order_pipeline`.

---

## Verified safe / no finding (consolidated)

The following claims were specifically checked, with concrete evidence, and
found to hold — listed here so remediation work doesn't waste effort
re-litigating what's already sound:

- **Position sizing cannot exceed what was requested, in any code path, including exception-handler fallbacks.** `src.quant.position_sizing.cap_requested_contracts` uses `contracts = min(requested_contracts, max_allowed_contracts)` — arithmetically incapable of returning more than the deterministic risk-budget cap. `RiskDecisionResult.approved_contracts` is set from `sized.contracts` in exactly one place; every reject/halt path (including the outer `except Exception` backstop in `evaluate_trade_proposal`) sets it to `None`.
- **No LLM output schema smuggles a numeric field into an authoritative position.** Of 16 LLM output schemas, only `TradeProposal`/`OptionLeg` carry numeric fields — by explicit design, since `TradeProposal` is declarative intent, independently repriced and resized by Python before anything executes. The other 14 schemas (`PortfolioDecision`, `DevilsAdvocateReview`, `StrategyResearchReview`, etc.) carry zero numeric fields.
- **Malformed or adversarial LLM tool-call output fails closed everywhere traced.** `src.llm.client.validate_tool_response` rejects anything but a single, correctly-named, dict-shaped `tool_use` block passed through Pydantic validation; any `ValidationError` becomes `LLMOutputError`, which is never caught/swallowed anywhere in the traced call graph (`devils_advocate.py`, `portfolio_manager.py`, `strategy_research.py`, `pipeline.py`) — it always surfaces as a clean pipeline rejection.
- **No fabricated market/account/fill data reaches a later call as ground truth.** `QuantitativeAnalysisContext`/`PortfolioStateContext` are constructed in exactly one production location, from the deterministic `QuantitativeAnalysis` and a caller-supplied `Portfolio` — never from LLM output.
- **Devil's Advocate is structurally independent of the Portfolio Manager.** No import of `src.llm.portfolio_manager` exists in `devils_advocate.py`; no parameter anywhere in its input model could carry a `PortfolioDecision`.
- **Black-Scholes core, probability-of-profit, and all three strategies' max-profit/max-loss/breakeven formulas match textbook definitions.** Spot-checked directly; the only expected-value simplification (`_binary_expected_value`'s two-outcome approximation) is explicitly, honestly disclosed in its own docstring, not a silent shortcut.
- **`max_drawdown`/`longest_drawdown_days` peak-tracking is correct** — no missed peaks or troughs under any tie-handling scenario checked.
- **NaN/Infinite value propagation is well-guarded throughout the quant/risk/backtest stack** — Black-Scholes clamps `t`/`sigma` floors before every division; `_require_iv`/`_underlying_spot` reject non-finite inputs at the boundary; `QuantitativeAnalysis` itself has a field validator rejecting any non-finite numeric field; Sharpe/Sortino explicitly return 0.0 rather than dividing by zero variance; `trade_statistics`' `inf` profit-factor case is explicitly labeled at its one display site, never silently formatted as a bogus number.
- **Zero-quote / crossed-option-market division-by-zero is guarded everywhere checked** — `.mid` properties never divide by a value that can be zero; every spread-percentage computation site gates on `mid > 0` first; `RiskLimitsConfig.max_bid_ask_spread_pct`'s own `<= 1.0` bound makes a no-bid contract's spread (always exactly 200%) structurally unable to pass liquidity screening regardless of configuration.
- **Missing strike/expiration data fails closed with typed exceptions, never a crash or silent substitution**, in both the live-trading path (`ContractNotFoundError` → clean `REJECT_INVALID_CONTRACT`) and the backtest path (`None` match → `ValueError` on close, or a skipped entry).
- **No Fidelity credentials, cookies, session tokens, unofficial endpoints, or browser-automation code exist anywhere in this repository** (FS-001), **`FidelityManualProvider` has no order-submission capability of any kind** (FS-002), **`execution_mode` has no runtime override mechanism** (FS-003), and **no LLM schema, context object, or agent tool can influence broker capabilities or execution mode** (FS-004).
- **`RISK_APPROVED` cannot reach `FILLED` without an explicit `ExecutionConfirmation`** (TS-001), and **PaperBroker fills and Fidelity real fills are disjoint types with zero conversion path between them anywhere in production code** (TS-002).
- **Multi-leg fills within one `PaperBroker` order are always atomic** — one `fillable_quantity` decision (from the worst leg's own liquidity) is applied to every leg in the same call; there is no path where two legs of one order end up at different filled quantities from a single fill event (see OP-003 for the separate, narrower concern about unvalidated *declared* per-leg quantities on order construction).
- **Assignment/exercise cash-flow sign and magnitude are correct** in the underlying `settle_leg`/`settle_position` formula itself, and agree exactly between the backtest and live-paper implementations — the bug in OP-001 is entirely in how the backtest engine *consumes* that correct output (dropping the share side), not in the settlement math itself.
- **Reconciliation's position-matching logic is correct** (no double-matching of one internal position to two confirmed positions or vice versa) — its only weakness (SY-006) is that a correct comparison result is never acted on.

---

*End of audit, as originally written (Step 17). Step 17B (see the
remediation update at the top of this document) subsequently fixed all 10
CRITICAL/HIGH findings, each with a regression test proving the fix, without
weakening any existing test — the full suite (1531 tests) passes. Step 22
(see that remediation update at the top of this document) subsequently
fixed two more findings — OP-003 and FS-005, both with regression tests,
without weakening any existing test — the full suite (2388 tests) passes.
All remaining MEDIUM/LOW findings remain open and unremediated; this
document continues to serve as their audit record pending a future
remediation step.*
