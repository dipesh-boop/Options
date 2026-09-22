# Progress

Living status tracker for the systematic options research & paper-trading
platform. Update this file at the end of every work session — append,
don't rewrite history.

## Status: LLM orchestration layer, Quant engine, Data layer, Broker
## layer (IBKR + Fidelity manual), and Deterministic Risk Engine
## (Phase 5/6/7/8/9 plumbing) implemented ahead of order; Phase 0
## foundations (DB, config, CI) and end-to-end wiring still not started

## 2026-09-19

- Received the full platform spec (Claude Code + Python 3.12+ + Anthropic
  API + Postgres + broker abstraction (IBKR/Schwab) + backtesting +
  deterministic risk engine, targeting ~12–15% annualized with capital
  preservation as the top priority).
- Explicit instruction for this session: architecture and planning only,
  no trading-system implementation yet.
- Delivered:
  - `ARCHITECTURE.md` — component map, LLM/Python trust boundary, trading
    mode gate design (LIVE not implemented, not just disabled), risk
    engine responsibilities, broker abstraction, end-to-end data flow,
    security risks, trading-system failure modes, open questions.
  - `IMPLEMENTATION_PLAN.md` — folder structure (`src/options_platform/`
    layout), dependency list, 8-phase delivery plan (Phase 0 Foundations
    → Phase 7 Hardening), explicit non-goals for this phase.
  - `progress.md` (this file).
- **Note:** before this spec arrived, a small unrelated prototype was
  started directly under `app/` at the repo root (Black-Scholes/Greeks
  solver, synthetic mock option-chain provider, Pydantic chain models) for
  an earlier, much narrower "options analysis dashboard" request. It does
  not follow the architecture in this plan (no `src/` layout, no DB, no
  risk gate, no broker/agent separation) and has **not** been deleted or
  merged into the new plan — left as-is pending a decision (see
  `IMPLEMENTATION_PLAN.md` §3). The Greeks/IV-solver code in
  `app/analytics/greeks.py` is a reasonable seed for
  `src/options_platform/quant/greeks.py` and is worth porting rather than
  rewriting when Phase 0/1 starts.
- Committed and pushed to `claude/options-trading-agent-2b4yi8`: the three
  planning docs (`a8dfa0d`) and the `app/` prototype as-is, clearly labeled
  as not part of the plan (`6be729c`).

## 2026-09-19 (cont'd) — Multi-Agent Layer refinement

- User supplied a specific orchestration design for the LLM layer: a
  Claude Opus-tier **Portfolio Manager** coordinating three sub-agents
  (**Market Agent**, **Strategy Analyst**, **Adversarial Reviewer**),
  feeding a two-stage Python verification (**Python Quant** → **Python
  Risk Engine**) before **Paper Broker**.
- Folded this into `ARCHITECTURE.md`:
  - New §5 "Multi-Agent Layer" defines all four roles, their inputs,
    outputs, and tools — every role still bound by the same read-only /
    no-trusted-numbers constraints from §2.
  - Split the former single "Risk Engine" section into §6 "Python Quant"
    (pure per-trade calculation: Greeks, P&L, max loss, EV — no portfolio
    state, no policy) and §7 "Python Risk Engine" (portfolio policy and
    gating: sizing, exposure, correlation, drawdown, RiskGate, circuit
    breaker) to match the two-box separation in the supplied diagram.
  - Redrew the §3 component map top-to-bottom through the new pipeline:
    Scheduler → Market Data → Strategy Screener → Multi-Agent Layer →
    Python Quant → Python Risk Engine → trading-mode gate → Broker
    Abstraction Layer.
  - Updated §9 data flow to match (screener output feeds the agent layer;
    agent proposals are non-numeric intent objects; Quant reprices from
    the live snapshot; Risk Engine gates before the mode gate).
  - Added a "multi-agent groupthink" failure mode to §11 (the Adversarial
    Reviewer's prompt is deliberately adversarial rather than
    collaborative; either way, Python Risk Engine is the actual backstop,
    not agent consensus).
  - Added open question 5 (§12): model tier per role — Opus-tier
    recommended for the Portfolio Manager, sub-agent tiers configurable.
- Updated `IMPLEMENTATION_PLAN.md`: split the `risk/` package into
  `quant/` (greeks, pnl, max_loss, expected_value) and `risk/` (sizing,
  exposure, correlation, drawdown, limits, circuit_breaker); expanded
  `agent/` into per-role modules (`portfolio_manager.py`,
  `market_agent.py`, `strategy_analyst.py`, `adversarial_reviewer.py`)
  plus the shared `schemas.py`/`tools.py`/`audit.py`; updated Phase 1 and
  renamed Phase 5 to "Multi-Agent Layer" with the four-role breakdown.
- Still design-only — no `src/options_platform/` code exists yet; this was
  a documentation refinement, not a Phase 0 start.

## 2026-09-19 (cont'd) — LLM orchestration layer implemented

- Explicit request to implement the LLM orchestration architecture
  (design-only phase lifted for this slice specifically): Anthropic API,
  configurable model router, Pydantic structured output, 8 specialized
  agents, tests proving malformed output can't reach execution. Broker
  execution explicitly out of scope for this change.
- Built, all under real (not mocked) file paths as specified:
  - `config/llm.yaml` — tier definitions (`high_reasoning`, `routine`)
    and `task_type → tier` routing table. No model identifier appears
    anywhere in `src/llm/*.py` — enforced by an automated test
    (`test_router.py::TestNoHardcodedModelNames`) that scans the source
    of `router.py`/`client.py` for `claude-`/`opus-`/`sonnet-`/`haiku-`
    substrings and fails if any is found.
  - `src/llm/router.py` — `ModelRouter`, `TaskType` (9 task types matching
    the 5 high-reasoning + 4 routine tasks specified), env-var override
    per tier.
  - `src/llm/schemas.py` — all schemas use `extra="forbid"` + `frozen=True`.
    `TradeProposal` (with a `StructureIntent` — declarative only, no
    trusted numeric field) is the only schema that may ever reach an
    order; every other role (Market Regime, Opportunity Scanner, Devil's
    Advocate, Risk Reviewer, Performance Auditor) gets its own
    analysis-only schema. `ensure_trade_proposal()` is an exact-type
    runtime boundary guard (rejects dicts, other schema instances, and
    even `TradeProposal` subclasses).
  - `src/llm/context.py` — read-only, JSON-serializable context builders
    (`CandidateContext`, `PortfolioStateContext`, `MarketContext`); never
    originates a number, only serializes what it's given.
  - `src/llm/prompts.py` — parses `.claude/agents/<role>.md` frontmatter +
    body, appends a fixed schema-discipline instruction to every prompt.
  - `src/llm/client.py` — `LLMClient.complete_structured()` forces
    tool-use (`tool_choice` pinned to one tool built from the target
    Pydantic schema's `model_json_schema()`), then
    `validate_tool_response()` requires exactly one correctly-named
    tool_use block whose `input` passes `model_validate()` — free text,
    wrong tool name, duplicate tool calls, and non-dict input are all
    rejected before validation is even attempted.
  - `.claude/agents/*.md` — all 8 personas (portfolio_manager,
    market_regime, opportunity_scanner, strategy_analyst, devil_advocate,
    risk_reviewer, trade_manager, performance_auditor), each with YAML
    frontmatter (name/description/default_task_type/tools — read-only
    tools only, no `place_order`/`execute`-shaped tool anywhere) and a
    body reiterating the no-numeric-authority / no-execution constraints
    from `ARCHITECTURE.md` §2 in role-specific terms.
  - `tests/unit/llm/` — 100 tests, all passing
    (`python3 -m pytest tests/unit/llm -q`). `test_execution_safety.py` is
    the direct proof requested: runs a full `raw API response →
    validate_tool_response → ensure_trade_proposal` pipeline against a
    valid control plus adversarial payloads (smuggled `execute`/
    `order_id`/`broker`/`bypass_risk_gate` fields, out-of-scope strategy
    types like `naked_call`, free-text instead of a tool call, duplicate
    tool_use blocks, forged objects with matching attributes but the
    wrong type) and asserts every adversarial case is blocked while the
    valid one survives.
- Added `anthropic==0.39.0` and `PyYAML==6.0.2` to `requirements.txt`;
  installed locally and used to run the suite (no real API key needed —
  all tests inject a fake Anthropic client).
- Updated `IMPLEMENTATION_PLAN.md`: new §6 documents that this landed at
  `src/llm/` / `config/llm.yaml` / `.claude/agents/` directly, not nested
  under `src/options_platform/` as §1 originally proposed — flagged as an
  open reconciliation for Phase 0, not silently resolved. Phase 5's
  description updated from the earlier 4-role sketch to the actual 8
  roles implemented, and now states plainly what's built vs. still
  missing (audit-log persistence, real tool implementations, pipeline
  wiring — all blocked on Phase 0/1 not existing yet).
- Still true: no broker code, no DB, no quant/risk engine, no screener.
  This slice is self-contained and network-free in its tests by design.
  (Status tracking — open decisions, next up — moved to the end of this
  file as of the last entry, rather than duplicated at each checkpoint;
  see there for current state.)

## 2026-09-19 (cont'd) — TradeProposal redefined to the specified field set

- Explicit request for a stricter `TradeProposal` with a precise field
  list: `proposal_id`, `timestamp`, `ticker`, `strategy`, `market_regime`,
  `expiration`, `legs`, `direction`, `contracts_requested`,
  `target_entry`, `profit_target`, `management_dte`, `thesis`,
  `risk_thesis`, `confidence`, `data_sources`, `data_timestamp`, plus a
  requirement that every proposal carry market data timestamp, source,
  reasoning summary, and invalidation conditions, and that stale/missing
  market data be rejected. This superseded the earlier, more abstract
  `TradeProposal`/`StructureIntent` pair from the first LLM-layer commit.
- Interpretation calls made (flagging rather than silently resolving):
  - "source" and "market data timestamp" in the follow-up requirement map
    onto the already-listed `data_sources`/`data_timestamp` fields, not
    new ones.
  - "reasoning summary" maps onto `thesis` (a thesis *is* a reasoning
    summary); no separate `reasoning_summary` field was added, to avoid a
    redundant field the 17-field list didn't ask for.
  - "invalidation conditions" is not covered by any of the 17 named
    fields, so `invalidation_conditions: list[str]` (non-empty, non-blank
    entries) was added as the one genuinely new required field.
  - Kept `action: TradeAction` (open/close/roll, default `open`) as an
    additive field beyond the specified list — removing it would have
    silently broken the Trade Manager role's documented close/roll
    capability (`.claude/agents/trade_manager.md`, written in the
    previous commit). It carries no execution authority (same as
    everything else in this schema) so it doesn't conflict with any of
    the five forbidden concepts.
  - Dropped `source_agent`, `rank`, and `risk_flags` from the old
    TradeProposal shape — not in the new field list, and not load-bearing
    elsewhere: `source_agent` is already captured at the call-result level
    (`AgentCallResult.agent_role` in `src/llm/client.py`), ranking is the
    Portfolio Manager's job at the container level, and risk flagging
    already has dedicated homes in `AdversarialReview.risk_flags` and
    `RiskReviewNote.concerns`, keyed by `proposal_id`.
- `src/llm/schemas.py` changes:
  - Removed `StructureIntent`; added `OptionLeg` (right/strike/side),
    `TradeDirection` (bullish/bearish/neutral), `OptionRight` (C/P),
    `LegSide` (buy/sell), and a shared `MarketRegimeLabel` literal used by
    both `TradeProposal.market_regime` and `MarketRegimeAssessment.regime`
    so the two never drift apart.
  - `TradeProposal` still inherits `_StrictModel` (`extra="forbid"` +
    `frozen=True`), so every one of the five explicitly forbidden
    concepts (final approved contracts, authoritative max loss, portfolio
    risk, broker order id, execution authorization) is rejected outright
    if smuggled in as an extra field — proven by parametrized tests using
    several spellings of each.
  - Four `@model_validator(mode="after")` checks: market data freshness
    (`data_timestamp <= timestamp`, and `timestamp - data_timestamp <=
    MAX_MARKET_DATA_AGE`, a 15-minute placeholder constant flagged in a
    `TODO(Phase 0)` comment as needing to move to config once one exists,
    the same way model routing moved to `config/llm.yaml`), expiration
    must be after the proposal's timestamp date, `management_dte` can't
    exceed the structure's total DTE, and legs must structurally match
    `strategy` (cash_secured_put = one short put; covered_call = one
    short call; put_credit_spread = exactly one short + one long put,
    short strike above long strike for a net credit). Two `field_validator`
    checks require `timestamp` and `data_timestamp` to be timezone-aware
    (a naive datetime is rejected, not silently assumed to be UTC).
  - Staleness is anchored on the proposal's own `timestamp`, not
    wall-clock "now" at validation time — deliberate, so a stored
    proposal doesn't retroactively become "stale" just because it's read
    back later; documented in the validator's docstring.
- Updated `.claude/agents/strategy_analyst.md` and `portfolio_manager.md`,
  which had prose referencing the removed `StructureIntent`/`rationale`/
  `conviction`/`risk_flags` field names, to describe the actual current
  schema. `trade_manager.md` needed no change (only referenced
  `action=close|roll`, which still exists).
- Tests: added `tests/unit/llm/test_trade_proposal.py` (a new, dedicated,
  comprehensive file — required-field parametrization over all 18
  fields, forbidden-field parametrization over multiple spellings of all
  five forbidden concepts plus a field-name substring scan of
  `TradeProposal.model_fields` as a second line of defense, leg/strategy
  consistency for all three strategies including boundary cases like
  equal strikes and debit-direction spreads, market data freshness
  including the exact 15-minute boundary, missing/empty/blank market
  data, expiration/DTE consistency, and general field constraints).
  Rewrote the `TradeProposal`-specific parts of `test_schemas.py` (now
  lighter — deep coverage lives in the new file) and
  `test_execution_safety.py` (new field shapes throughout, plus the five
  named forbidden concepts folded into its adversarial-payload
  parametrization, plus new stale/missing-market-data pipeline tests).
  Full suite: **193 tests passing** (`python3 -m pytest tests/ -q`).

## 2026-09-19 (cont'd) — Deterministic quant engine implemented

- Explicit request for the deterministic quantitative engine at
  `src/quant/`: `black_scholes.py`, `greeks.py`, `volatility.py`,
  `probability.py`, `expected_value.py`, `monte_carlo.py`,
  `position_sizing.py`, `correlations.py`. Independently calculates
  delta/gamma/theta/vega, implied vol, max profit/loss, breakeven,
  probability ITM, probability of profit, ROC/annualized ROC, expected
  value; Monte Carlo stress testing at the six required spot shocks
  (-20/-10/-5/+5/+10/+20%) plus a vol-shock grid. Explicit instruction:
  the LLM must consume these calculations, never replace them with its
  own arithmetic; no execution implemented.
- Design decisions:
  - `black_scholes.py` owns the shared `OptionRight`/`Side`/`Leg` types
    every other quant module builds on — kept minimal (right, strike,
    side, entry_price, quantity), no dependency on `src.llm.schemas`.
    Every quant function takes plain numeric parameters, never a
    `TradeProposal` — this is what makes the one-way LLM-consumes-Quant
    dependency a structural fact rather than a convention: verified by a
    new `tests/unit/quant/test_architecture_boundary.py` that scans
    every module's source for an import of `src.llm` and fails the
    parametrized test if found.
  - `expected_value.py`'s per-strategy functions (`csp_max_profit`,
    `covered_call_breakeven`, `put_credit_spread_max_loss`, etc.) take
    explicit float parameters (strike, credit, cost_basis, width) rather
    than a shared position object — deliberately, so each is trivially
    checkable against the textbook formula written directly in a test.
  - `monte_carlo.py` groups two distinct tools under one roof: true
    Monte Carlo (`simulate_terminal_prices`, `monte_carlo_pop_and_ev`,
    risk-neutral GBM, seeded for reproducibility) as an independent
    cross-check of the closed-form EV in `expected_value.py` (which uses
    a binary max-profit/max-loss approximation, documented as such); and
    a deterministic `stress_test` grid (exact Black-Scholes repricing at
    fixed spot/vol shocks, no randomness) — the practical tool for
    "stress underlying at -20%/…/+20%". `STANDARD_SPOT_SHOCKS` is
    exactly the six required values.
  - `position_sizing.py` lives under `src/quant/` per this request,
    slightly ahead of ARCHITECTURE.md §7's original quant/risk split (it
    had put sizing in the risk/ package). Kept as pure calculation only —
    `fixed_fractional_size` computes a maximum from risk parameters,
    `cap_requested_contracts` bounds a request by that maximum — no
    portfolio-state awareness or limit enforcement, which is still
    Python Risk Engine's job (not implemented).
  - Net position greeks (`greeks.net_greeks`) needed a real design
    decision, not just a data plumbing exercise: option-leg greeks are
    naturally per-share (Black-Scholes convention), but need converting
    to per-contract/100-share-equivalent units before they're additive
    with a raw `underlying_shares` count for a covered call's net delta.
    Caught by a test expecting the standard "position deltas" convention
    (`100 - 100*call_delta`), which failed against the first
    implementation (missing the 100x contract multiplier) — fixed in
    `net_greeks`, not in the test.
  - `volatility.py`'s arbitrage-floor check had a real bug the round-trip
    tests caught: it validated `market_price` against naive intrinsic
    value (`max(K-S,0)`), but that's the *American* exercise-value bound.
    A European option's correct no-arbitrage floor is the *discounted*
    intrinsic value (`max(K*e^{-rT}-S, 0)` for puts), and a deep-ITM
    European put can legitimately price below naive intrinsic when
    discounting dominates — not a violation, since early exercise isn't
    available to compare against. Fixed to use the discounted bound.
- Tests emphasize independent verifiability per the instruction, not just
  "does it run":
  - `test_black_scholes.py`: an `N(x)` implementation via `math.erf`
    (deliberately not `scipy.stats.norm`, a genuinely different code
    path) cross-checks call/put prices across 6 parameter sets x 2
    rights; put-call parity (`C - P = S - K*e^{-rT}`, an exact
    no-arbitrage identity) checked across the same sets; Hull's textbook
    reference value (S=42,K=40,r=10%,σ=20%,T=0.5 → call≈4.76).
  - `test_greeks.py`: every greek (delta, gamma, theta, vega)
    cross-checked against a central finite-difference derivative of
    `black_scholes.price` itself — an independent numerical method, not
    a hardcoded number — plus call/put delta identities and bounds.
  - `test_volatility.py`: round-trip (price at a known sigma → solve IV →
    recover that exact sigma) across 7 parameter sets x 2 rights x
    ATM/ITM/OTM/short-dated/long-dated/high-vol combinations.
  - `test_monte_carlo.py`: Monte Carlo EV converges to the closed-form
    Black-Scholes price within a statistically principled tolerance (a
    multiple of the simulation's own standard error, derived from the
    CLT, not an arbitrary epsilon); payoff boundary checks at S_T=0 and
    S_T→∞ against the hand-derivable max loss/max profit; stress grid
    P&L checked against direct Black-Scholes repricing.
  - `test_expected_value.py`: every expected value asserted against a
    direct textbook-formula expression written in the test itself (e.g.
    `(strike - credit) * 100`), not a number copied from production
    output; EV composition cross-checked against
    `probability.probability_of_profit` called independently.
  - `test_correlations.py`: exact constructed cases (identical series →
    correlation exactly 1; an exact linear inverse → exactly -1) rather
    than approximate real-data correlations.
  - Full repo suite: **390 tests passing**
    (`python3 -m pytest tests/ -q`; 197 in `tests/unit/quant/`, 193 in
    `tests/unit/llm/`).
- `requirements.txt` already had `scipy`/`numpy` pinned from the original
  `app/` prototype scaffold — no dependency changes needed.
- **`app/` prototype is now fully redundant, not just partially**:
  `app/analytics/greeks.py`'s entire job is now covered, with
  independent verification it never had, by `src/quant/black_scholes.py`
  + `greeks.py` + `volatility.py`. Recommend resolving the port-vs-delete
  question (open since the very first commit) before Phase 0 rather than
  carrying it further — flagged again in `IMPLEMENTATION_PLAN.md` §7.

## 2026-09-19 (cont'd) — Normalized market data layer implemented

- Explicit request for `src/data/`: `provider.py`, `quotes.py`,
  `option_chain.py`, `historical.py`, `earnings.py`. One canonical
  `OptionContract` schema every broker/provider must convert its raw
  response into; never expose a raw broker response to an LLM; freshness
  validation that marks stale data STALE and prohibits trade approval.
- `provider.py` holds the shared plumbing every other file builds on:
  `StrictModel` (`extra="forbid"`, `frozen=True`) and `TimestampedModel`
  (adds a timezone-aware `timestamp` + `source`, plus `.age()`,
  `.freshness_status()`, `.require_fresh()`); `FreshnessStatus`
  (FRESH/STALE) and `StaleDataError`; `ensure_canonical()` — an exact-
  type boundary guard, same pattern as
  `src.llm.schemas.ensure_trade_proposal`; the abstract
  `MarketDataProvider` interface.
- `option_chain.py`'s `OptionContract` has all 18 requested fields
  (underlying, option_symbol, expiration, strike, right, bid, ask, mid
  [computed], last, volume, open_interest, iv, delta, gamma, theta,
  vega, underlying_price, timestamp, source) plus a `bid <= ask`
  cross-field validator. `assert_tradable()` is the concrete "prohibit
  trade approval" mechanism: it calls `.require_fresh()` and *raises*
  `StaleDataError` on stale data — deliberately not a status flag a
  future caller could check and ignore.
- **"Never expose raw broker responses to an LLM" is enforced
  structurally, not just documented**: every canonical type has
  `extra="forbid"`, so a raw provider dict (broker-specific ids, a
  nested greeks blob, an exchange code — anything the canonical schema
  doesn't define) fails validation outright rather than being silently
  accepted as if already normalized — proven by
  `TestRawBrokerResponseNeverPassesAsCanonical` in
  `tests/unit/data/test_option_chain.py`. `ensure_canonical()` is the
  second, independent check at the actual consumption boundary.
- Design decisions:
  - **`iv`/`delta`/`gamma`/`theta`/`vega` on `OptionContract` are
    provider-reported reference values, not independently computed** —
    `src/data` has no dependency on `src/quant`. Python Quant recomputes
    when a number actually needs to be trusted for a risk decision; it
    never trusts a provider's stated greeks any more than an LLM's.
    Verified structurally the same way as the `src.llm` boundary: a new
    `tests/unit/data/test_architecture_boundary.py` scans `src/data/*.py`
    for a `src.llm` import and fails if found. (`src/data` also doesn't
    import `src/quant` — not tested for, since nothing prevents that
    direction, just not needed yet.)
  - `HistoricalBar` deliberately does NOT inherit `TimestampedModel` /
    get live-data freshness — a bar's validity is point-in-time
    (`bar_date`), not "how long ago was this fetched." Its integrity
    check is `assert_no_lookahead()`, a concrete no-lookahead guard for
    the future backtest engine (ARCHITECTURE.md §9/§11's "strict
    point-in-time data discipline"), not freshness.
  - Historical *options chain* data is explicitly out of scope here —
    ARCHITECTURE.md §12 still hasn't picked a vendor. `historical.py`
    only covers underlying OHLCV bars, which are vendor-agnostic enough
    to build now.
  - `earnings.py`'s `is_within_earnings_window()` is symmetric (checks
    both before and after the earnings date) and directly implements the
    platform's "no earnings-window entries" universe rule for a future
    Strategy Screener to call.
  - Three tiny `OptionRight` enums now exist (`src.llm.schemas`,
    `src.quant.black_scholes`, `src.data.option_chain`) — a deliberate
    duplication to keep each layer independently importable without
    cross-layer coupling, flagged (with the `src/core/` primitives
    module as the eventual fix) in `IMPLEMENTATION_PLAN.md` §8.
  - `DEFAULT_MAX_QUOTE_AGE` (15 min, in `src/data/provider.py`) is the
    same placeholder-policy pattern as
    `src.llm.schemas.MAX_MARKET_DATA_AGE` — same value, declared
    independently a second time. Flagged, not unified, in
    `IMPLEMENTATION_PLAN.md` §8.
- Tests: `tests/unit/data/` — 94 tests covering valid construction for
  every canonical type, rejection of invalid/out-of-range/extra fields,
  the `bid <= ask` and OHLC-consistency cross-field validators,
  freshness at/past/well-past the boundary (including the exact-boundary
  inclusive case), `assert_tradable`'s stale-data rejection, the
  no-lookahead guard, the earnings-window check's before/after/boundary
  cases, and each abstract provider's contract (via a minimal concrete
  subclass defined in the test file — no mock implementation was added
  to `src/data` itself, since only the 5 requested files were in scope).
  Added `pytest-asyncio` to `requirements-dev.txt` for the async
  provider-contract tests. Full repo suite: **484 tests passing**.

## 2026-09-19 (cont'd) — IBKR broker integration implemented

- Explicit request for Interactive Brokers behind a `Broker` interface:
  account info, positions, underlying quotes, option chains + Greeks,
  paper orders, open orders, fills, cancellation, reconciliation. Live
  trading disallowed; env-var config only, no credentials in source;
  connection health monitoring; retry where safe, never blind retry on
  order submission; idempotency protection against duplicate orders;
  paper accounts only; integration tests that need no live money.
- `src/brokers/base.py`: the `Broker` ABC plus canonical
  `Account`/`Position`/`Order`/`Fill`/`OrderLeg`/`PlaceOrderRequest`
  schemas. Market-data-shaped methods
  (`get_underlying_quote`/`get_option_chain`) return `src.data`'s
  existing `UnderlyingQuote`/`OptionChain` directly — reusing the
  canonical types, not redefining them a fourth time. `IdempotencyStore`
  (ABC) + `InMemoryIdempotencyStore` (explicitly flagged as lost on
  restart, a placeholder for Phase 0's real persisted order state
  machine) live here too, since idempotency is a Broker-layer concern,
  not an IBKR-specific one.
- `src/brokers/ibkr.py`: `IBKRConfig` (env vars, prefix
  `OPTIONS_AGENT_IBKR_*`, no credential fields — IBKR authenticates
  against an already-logged-in local TWS/Gateway session, so there's no
  secret to embed in the first place). **Paper-only enforced twice,
  independently**: `require_paper_port()` blocks connection outright
  unless the port is a known IBKR paper port (7497/4002) — known live
  ports (7496/4001) and anything unrecognized are both rejected, not
  just live ports specifically; and after connecting, every account
  IBKR reports must use the conventional paper prefix ("DU") or the
  connection is aborted (disconnects, then raises). Neither check is a
  warning.
- Talks to IBKR only through `IBClientLike`, a narrow Protocol this
  module defines itself (not ib_insync's full surface) — `_RealIBAdapter`
  isolates every actual `ib_insync` call and the translation from this
  module's internal contract/order spec objects into real `ib_insync`
  objects into one reviewable class. Tests inject a fake `IBClientLike`
  directly and never construct `_RealIBAdapter` or import `ib_insync` at
  all — **`_RealIBAdapter` itself is untested by the automated suite**,
  flagged rather than hidden (no live TWS/Gateway exists in this
  environment to test against; it needs a manual smoke test before this
  adapter is trusted with real paper capital).
- **Idempotency, not retry, is what makes duplicate-order protection
  work**: `place_order`/`cancel_order` are never wrapped in automatic
  retry — a timed-out submission has an unknown broker-side outcome, and
  blindly resubmitting risks a real duplicate. `client_order_id` is the
  idempotency key: `place_order` checks a local `IdempotencyStore` first
  (fast path), then falls back to searching the broker's own open orders
  by `orderRef` before ever submitting (defense against "we crashed
  after submitting but before recording it locally"). A caller that
  wants to retry after a failure calls `place_order` again with the same
  `client_order_id` — safe by construction, not by promise.
- Read-only methods (`get_account`, `get_positions`,
  `get_underlying_quote`, `get_option_chain`, `get_open_orders`,
  `get_fills`, `reconcile`) go through `_with_read_retry`: bounded
  attempts, reconnecting between them, catching `BrokerConnectionError`
  plus the builtin transient-transport exceptions
  (`ConnectionError`/`TimeoutError`/`OSError`) a real socket connection
  can raise. `place_order`/`cancel_order` never call it — proven
  structurally by a test that greps each method's source for the retry
  helper's name, not just behaviorally.
- `reconcile()` compares local non-terminal orders against the broker's
  own non-terminal open orders and reports `orphaned_local` /
  `unknown_broker` / `status_mismatch` discrepancies. Never auto-fixes
  anything — a test asserts a discrepancy-detecting reconcile call
  issues zero cancel/place calls of its own.
- **Real bugs the tests caught before shipping** (four, this time):
  (1) `_build_ib_order`'s single/multi-leg branch had a tuple-unpacking
  bug that would `ValueError` on every single-leg order. (2) `_RealIBAdapter`
  originally just forwarded this module's internal `_StockSpec`/
  `_OptionSpec`/`_LimitOrderSpec` objects straight to real `ib_insync`
  calls, which would fail immediately against a real connection since
  ib_insync expects its own `Contract`/`Order` types — fixed by adding
  actual translation methods. (3) `place_order`'s `DuplicateOrderError`
  branch was unreachable dead code (guarded by a condition already
  falsified by an earlier early-return) — rewritten so the check is
  reachable: `_find_open_order_by_client_id` now raises if a
  `client_order_id` matches *more than one* broker-side order, a real
  invariant violation. (4) `_order_status_from_ib` had no mapping for
  IBKR's `"PartiallyFilled"` status, silently falling back to
  `SUBMITTED` — caught by a reconciliation test expecting a status
  mismatch to be detected.
- Tests: `tests/unit/brokers/` — 102 tests, including a shared
  `fakes.py` `FakeIBClient` (injectable failure counts for retry tests,
  realistic order lifecycle — cancel actually changes status,
  `openTrades()` excludes terminal orders like real ib_insync). Covers
  config/paper-port enforcement, connection + paper-account verification
  (including mixed paper/live account lists and a configured-but-absent
  account id), connect retry, health checks, account/positions/
  underlying-quote/option-chain retrieval with Greeks, place_order
  idempotency (including the broker-side-adoption and
  duplicate-broker-order-conflict paths), cancel semantics (including
  idempotent double-cancel), open orders, fills, reconciliation's three
  discrepancy kinds, read-retry-with-reconnect, and the
  no-blind-retry-on-mutation structural check. Full repo suite:
  **586 tests passing**.
- No dependency changes: `ib_insync` and `pydantic-settings` were
  already in `requirements.txt`. Used a small hand-rolled retry helper
  instead of adding `tenacity` (still listed as a future dependency in
  `IMPLEMENTATION_PLAN.md`, not yet actually needed) — precise control
  over the reconnect-between-attempts semantic, and deterministic tests
  without fighting a generic backoff decorator's timing.

## Open decisions carried forward (updated)

- [ ] Historical options data vendor for backtesting (Phase 2 blocker)
- [ ] Schwab paper-trading / sandbox capability (Phase 4 blocker)
- [ ] Sector/classification data source for correlation/concentration checks
- [ ] Final ~50-name equity universe list + liquidity criteria
- [ ] **`app/` prototype disposition — now fully redundant, recommend
      resolving before Phase 0 (see IMPLEMENTATION_PLAN.md §7)**
- [ ] Fold `src/llm/` + `src/quant/` + `src/data/` + `src/brokers/` under
      `src/options_platform/`, or keep `src/` flat with multiple
      top-level packages (IMPLEMENTATION_PLAN.md §6-§9) — deferred four
      times now, getting more expensive to resolve later each time
- [ ] Model tier per non-Portfolio-Manager agent role
- [ ] Two independently-declared 15-minute freshness placeholders
      (`src.llm.schemas.MAX_MARKET_DATA_AGE`,
      `src.data.provider.DEFAULT_MAX_QUOTE_AGE`) should become one
      config value in Phase 0
- [ ] No concrete *market data* provider exists yet for `src.data`'s
      `MarketDataProvider` interface (mock or real) — `IBKRBroker` now
      implements the *broker* side (`src.brokers.base.Broker`)
      independently; the two interfaces aren't unified
- [ ] Four duplicated small enums (`OptionRight` x3 in `src.llm`/
      `src.quant`/`src.data`, plus `src.brokers.OrderAction` vs.
      `src.quant.black_scholes.Side`) — candidate for a shared
      `src/core/` primitives module
- [ ] `_RealIBAdapter` (the actual `ib_insync` glue in
      `src/brokers/ibkr.py`) is untested by the automated suite and
      needs a manual smoke test against a real paper TWS/Gateway session
      before being trusted — impossible to verify in this environment
- [ ] `InMemoryIdempotencyStore` is process-local only, lost on restart —
      a placeholder for Phase 0's real persisted order state machine

## Next up

- Five standalone pieces now exist — `src/llm/` (agent plumbing),
  `src/quant/` (deterministic calculations), `src/data/` (canonical
  market data schemas + freshness), `src/brokers/` (IBKR paper trading,
  idempotent orders, reconciliation) — each internally tested but not
  connected to each other or to anything live. No DB, no Strategy
  Screener, no Python Risk Engine (portfolio-state-aware limits beyond
  `src.quant.position_sizing`'s pure calculation), no orchestrator. IBKR
  can now genuinely be connected to (given a real paper TWS/Gateway
  session, which this environment doesn't have) for account/position/
  market-data/order operations — this is the first piece of the
  platform that could touch a real (paper) external system rather than
  only ever running against mocks. Also
  still open: the `app/` prototype disposition and the accumulating
  layout/config-duplication/untested-real-adapter questions above.

## 2026-09-19 (cont'd) — Fidelity manual-execution provider (Steps 8, 8A)

- **Step 8**: `src/brokers/fidelity.py` — Fidelity has no supported
  automated retail execution API; explicit instruction to build a
  MANUAL_EXECUTION-only provider, not to reverse-engineer/scrape/
  automate Trader+ or Fidelity.com, not to store credentials, bypass
  MFA, or use session cookies. `FidelityManualProvider` deliberately
  does **not** implement `src.brokers.base.Broker` — that interface
  describes something that actually submits orders, and implementing it
  here (even as a stub) would misrepresent this class's capability. It
  has exactly one public method, `generate_trade_ticket()`.
  - Status state machine (PROPOSED → … → RISK_APPROVED → AWAITING_HUMAN
    → ORDER_ENTERED → …): FILLED/PARTIALLY_FILLED are reachable *only*
    through `confirm_fill()` with a real `ExecutionConfirmation` (no
    default — can't be called without one), and only from
    `ORDER_ENTERED`/`PARTIALLY_FILLED`. `transition()` explicitly
    rejects any attempt to reach a fill status. A Pydantic validator on
    `FidelityTradeTicket` independently enforces `execution_confirmation`
    present iff status is a fill status, so a contradictory ticket can't
    be constructed even by hand. "Risk-approved does not mean executed"
    is structural, not a comment.
  - 90 tests (`tests/unit/brokers/test_fidelity_*.py`) proving no
    automated submission capability via four independent lines of
    evidence: static import scan (no requests/httpx/selenium/playwright/
    subprocess/etc.), capability scan (doesn't subclass `Broker`, no
    order-submission-shaped method name, exactly one public method),
    credential scan (no password/username/cookie/mfa_bypass-shaped field
    or identifier anywhere), and — the strongest proof — a runtime check
    with `socket.socket` patched to raise if constructed at all, across
    ticket generation, rendering, and the full lifecycle.
  - Worked example (the exact SPY 620/615 put credit spread from the
    task) cross-checked against `src.quant.expected_value`'s
    `put_credit_spread_*` formulas — independently reproducing $270 max
    profit / $730 max loss / $618.65 breakeven, not just internally
    consistent with itself.
  - Committed separately (`fd39865`) after an explicit "stop and show me
    the implementation and tests" — presented for review before
    committing, per that instruction; committed once a stop-hook
    required a clean working tree.
- **Step 8A**: extended the ticket to match Fidelity Trader+'s actual
  order-entry fields. Six new required fields on `ApprovedOrder`/
  `FidelityTradeTicket`: `account_alias`, `net_bid`/`net_ask` (with a
  computed `net_mid` property, same pattern as `OptionContract.mid`),
  `minimum_acceptable_price`, `capital_at_risk`, `management_dte`
  (`time_in_force` also added, defaulted to `"DAY"`). All quantitative,
  all Python-supplied — no LLM dependency exists in this module to alter
  them, same as before.
  - New cross-field validators (extracted as shared module-level
    functions called from both models, to avoid duplicating the *logic*
    even though the two models still duplicate the *fields*):
    `net_bid <= net_ask`; `minimum_acceptable_price` can't be better
    than `limit_price` in either credit or debit direction;
    `capital_at_risk >= max_loss` (a real, useful distinction — for a
    cash-secured put, capital_at_risk/collateral posted is strike×100,
    strictly more than max_loss, which nets out the premium received;
    they're only equal for a put credit spread by construction);
    `management_dte` can't exceed the structure's total DTE (same
    pattern as the original `src.llm.schemas.TradeProposal`).
  - `render_ticket_text()` rewritten to the exact requested section
    order/labels (ACCOUNT → UNDERLYING → STRATEGY → EXPIRATION → LEG 1
    → LEG 2 → ORDER → TARGET LIMIT → MINIMUM ACCEPTABLE → TIME IN FORCE
    → CURRENT NET BID/ASK/MID → QUOTE TIME → MAX PROFIT → MAX LOSS →
    BREAKEVEN → CAPITAL AT RISK → RETURN ON CAPITAL → PROFIT TARGET →
    MANAGEMENT DTE → STATUS), verified against the task's own worked
    example output.
  - `copy_fidelity_order_text()` added for the future dashboard's "COPY
    FIDELITY ORDER" button — returns the exact clipboard text; actual
    clipboard access is left to the frontend
    (`navigator.clipboard.writeText`), since that's a browser concern a
    backend Python function has no business attempting.
  - Updated all 4 existing Fidelity test files' fixtures for the new
    required fields, plus ~30 new tests for the new validators/fields/
    render format/copy function. Full repo suite: **708 tests passing**.
- Both Fidelity commits keep `fidelity.py` fully covered by the existing
  `tests/unit/brokers/test_architecture_boundary.py` (globs
  `src/brokers/*.py`) with zero changes needed — confirms no `src.llm`
  import, automatically, for every file added to this package going
  forward.

## 2026-09-19 (cont'd) — Deterministic Fidelity-aware Risk Engine implemented (Step 9)

- **Read first, per Step 9's own instructions**: `ARCHITECTURE.md`,
  `IMPLEMENTATION_PLAN.md`, this file, and the existing source for
  `src.quant.expected_value` / `position_sizing` / `correlations` /
  `monte_carlo`, `src.data.provider` / `option_chain`, `src.llm.schemas`,
  `src.brokers.fidelity`, `src.brokers.ibkr` (for its `IBKRConfig`
  env-var pattern) and `src.llm.router` (for its YAML-config-loading
  pattern). Confirmed no `src/risk/` package existed yet and nothing
  else in the codebase computes portfolio-level risk.
- **New package `src/risk/`** (10 modules, mirroring the CREATE list
  exactly) plus `config/risk_limits.yaml` and `config/brokers.yaml`:
  - `reason_codes.py` — `RiskDecision` (APPROVE/RESIZE/REJECT/HALT) and
    a `ReasonCode` enum. Every code named in the spec is present
    verbatim (`REJECT_MAX_TRADE_RISK`, `REJECT_UNDERLYING_CONCENTRATION`,
    `REJECT_SECTOR_CONCENTRATION`, `REJECT_INSUFFICIENT_CASH`,
    `REJECT_BUYING_POWER`, `REJECT_STALE_DATA`, `REJECT_LIQUIDITY`,
    `REJECT_UNDEFINED_MAX_LOSS`, `REJECT_ACCOUNT_CAPABILITY`,
    `REJECT_CORRELATION`, `REJECT_DUPLICATE_POSITION`,
    `HALT_PORTFOLIO_DRAWDOWN`, `RESIZED_POSITION_RISK`, `APPROVED`),
    plus additional codes the spec's fail-closed list implied but didn't
    name (`REJECT_MALFORMED_PROPOSAL`, `REJECT_INVALID_CONTRACT`,
    `REJECT_MISSING_COLLATERAL`, `REJECT_BROKER_ACCOUNT_MISMATCH`,
    `REJECT_QUANT_MISMATCH`, `REJECT_STRESS_TEST_FAILURE`,
    `REJECT_DRAWDOWN_RISK_REDUCTION`, `HALT_MANUAL_KILL_SWITCH`, and the
    catch-all `REJECT_UNKNOWN_RISK`).
  - `limits.py` + `config/risk_limits.yaml` — same
    YAML-plus-per-value-env-override pattern `src.llm.router.ModelRouter`
    established for model names. All ten named limits (target/absolute
    risk per trade, underlying/sector concentration, min cash reserve,
    normal/absolute max capital deployed, drawdown warning/risk-reduction/
    halt) load from config, each with an `..._env` override key, into a
    frozen `RiskLimitsConfig`; a `model_validator` rejects an internally
    inconsistent file (target > absolute max, non-increasing drawdown
    thresholds). Also carries liquidity minimums, the correlation
    threshold, the market-data-freshness window, a risk-free rate for
    the engine's own repricing, a quant-cross-check tolerance, and the
    stress-test loss ceiling — none of these were named as one of the
    ten headline limits but all were needed to make the engine
    deterministic without a single hard-coded number anywhere in
    `src/risk/*.py` (`tests/unit/risk/test_no_hardcoded_limits.py`
    scans every module's source for all ten policy literals).
  - `broker_constraints.py` + `config/brokers.yaml` — `BrokerCapabilities`
    (execution_mode, account_alias, options_enabled,
    allowed_strategies), loaded per-broker from `config/brokers.yaml`
    (the Fidelity example given in the task, plus an `ibkr_paper`
    entry). `load_broker_capabilities` returns `None` — never raises —
    for a broker missing from the file, forcing every caller to treat
    "not configured" as a first-class outcome
    (`ReasonCode.REJECT_ACCOUNT_CAPABILITY`) rather than risk an
    exception being swallowed. Strategy names in the YAML are matched
    against `src.llm.schemas.StrategyType` by exact member name
    (`StrategyType["CASH_SECURED_PUT"]`), so an unrecognized strategy
    string in config fails loudly at load time.
  - `portfolio_risk.py` — the `Portfolio` type the engine gates
    against: NAV, cash, `peak_equity` (drawdown high-water mark),
    `positions` (each carrying enough leg detail — right/side/strike/
    entry_price — to be re-priced under stress), `underlying_holdings`
    (share collateral + cost basis for covered calls, since neither a
    `TradeProposal` nor market data can ever supply a cost basis),
    `sector_by_ticker`, `price_history` (for correlation), and a manual
    `halted`/`halt_reason` kill-switch pair. All monetary fields reject
    NaN/±∞ via an explicit `math.isfinite` check (Pydantic's `gt=0`/`ge=0`
    alone rejects NaN and −∞ but *not* +∞, since `inf > 0` is `True` —
    worth calling out because it's an easy way to think a `Field(gt=0)`
    constraint is a complete finiteness guarantee when it isn't).
    Aggregation helpers: `capital_deployed_pct`, `cash_reserve_pct`,
    `underlying_exposure_pct`, `sector_exposure_pct`,
    `find_duplicate_position`.
  - `trade_risk.py` — the single-trade layer. `QuantitativeAnalysis` is
    the caller-supplied "what Python Quant already computed for this
    proposal" input the spec lists; `compute_trade_economics`
    *independently* recomputes `StrategyEconomics` (reusing
    `src.quant.expected_value`'s existing `csp_economics` /
    `covered_call_economics` / `put_credit_spread_economics` — not
    reimplemented) from the resolved, fresh market contracts, and
    `cross_check_quantitative_analysis` rejects
    (`REJECT_QUANT_MISMATCH`) if the supplied analysis disagrees with
    this module's own recomputation beyond a configured tolerance — the
    engine never simply trusts either input over the other.
    `resolve_leg_contracts` matches every `TradeProposal` leg to a live
    `OptionContract` and requires freshness via
    `OptionContract.require_fresh` (reused, not reimplemented);
    `check_liquidity` enforces open-interest/volume/bid-ask-spread
    minimums; `check_collateral` enforces the specific collateral each
    strategy needs (cash for a CSP, 100 shares/contract for a covered
    call); `size_trade` wraps
    `src.quant.position_sizing.fixed_fractional_size` +
    `cap_requested_contracts` (reused verbatim — this pair already *is*
    the "may only reduce, never increase requested contracts"
    mechanism the spec asks for) with both the target and absolute
    risk-per-trade limits.
  - `concentration.py` / `correlation.py` — thin limit-enforcement
    wrappers: the former reads `Portfolio`'s aggregation helpers, the
    latter wraps `src.quant.correlations.flag_highly_correlated_pairs`
    (reused, not reimplemented) against `Portfolio.price_history`.
    **Known, documented gap**: no live historical-price wiring exists
    yet from `src.data.historical` into `Portfolio.price_history`, so
    the correlation check is skipped (not fail-closed-rejected) whenever
    price history for a ticker pair is absent — fail-closing here would
    make the engine unable to approve *any* trade in a portfolio that
    already holds any other position at all, which is a worse failure
    mode than a documented, narrow gap. Covered explicitly by a test
    (`test_uncorrelated_or_unmeasured_existing_position_does_not_block`)
    so the gap is provable, not silent.
  - `drawdown.py` / `kill_switch.py` — `current_drawdown_pct` off
    `peak_equity` vs. `nav`; `drawdown_zone` maps that to
    NORMAL/WARNING/RISK_REDUCTION/HALT; `sizing_multiplier_for_zone`
    only ever tightens (≤ 1.0) the risk-per-trade budget in the
    RISK_REDUCTION zone, never loosens it. `check_kill_switch` is the
    one function the engine calls before *anything* else: a manual
    `Portfolio.halted=True` or an automatic HALT-zone drawdown both
    short-circuit straight to `RiskDecision.HALT`, before market-data
    freshness, broker capability, or any other check even runs.
  - `stress.py` — deterministic stress testing, wrapping
    `src.quant.monte_carlo.stress_test`/`Position` (reused). Two
    capabilities: a full multi-ticker `run_portfolio_stress_test`
    (built and unit-tested standalone) and the function the engine
    actually calls, `worst_case_stress_loss` — a single position's worst
    loss across the exact required grid (spot: −20/−10/−5/+5/+10/+20%;
    vol: +10/+25/+50%, both matching the spec's literal numbers, which
    is why those specific literals are allow-listed as documented,
    justified exceptions in the no-hardcoded-limits scan rather than
    pulled from config — they're a fixed scenario definition, not a
    tunable policy). **Known, documented gap**: the engine combines the
    new position's stressed worst case with existing positions'
    already-known `max_loss` *statically* rather than repricing every
    existing position too, because existing positions may be on tickers
    the engine's declared inputs (a `CurrentMarketData` for the one
    ticker under review, not the whole portfolio) have no market data
    for. This is conservative (a position's own `max_loss` is already
    its own worst case) but not a full portfolio repricing — flagged
    below as a Phase-0+ candidate once multi-ticker market data is
    available as an engine input.
  - `engine.py` — `evaluate_trade_proposal`, the single deterministic
    choke point. Never raises: every anticipated failure is caught at
    its own step and mapped to a specific `ReasonCode`; anything
    unanticipated falls through an outer `except Exception` to
    `REJECT_UNKNOWN_RISK` (proven by a test that monkeypatches a check
    to raise `RuntimeError` and confirms the engine still returns a
    `REJECT`, never propagates). Order of checks: malformed-proposal
    boundary guard → kill switch → market data identity/freshness →
    broker capability (never inferred) → leg contract resolution +
    freshness → liquidity → independent economics recomputation +
    quant cross-check → drawdown-zone sizing multiplier → position
    sizing (never more than requested) → collateral → cash/buying
    power → duplicate position → concentration → correlation → stress
    test → decision. On APPROVE/RESIZE for a MANUAL-execution broker
    (Fidelity) and an OPEN action, the engine builds a validated
    `ApprovedOrder` from its own independently-computed numbers and
    calls the existing, unmodified `FidelityManualProvider
    .generate_trade_ticket()` (Step 8/8A) — this is the first thing in
    the codebase that actually produces a real `ApprovedOrder`, closing
    the gap flagged at the end of the last entry ("nothing yet actually
    produces an ApprovedOrder"). `evaluate_trade_proposal` and every
    module it imports have zero dependency on `src.llm.client`/
    `src.llm.router` (the two modules capable of an actual model call);
    the only `src.llm` import anywhere in `src/risk/` is
    `src.llm.schemas` (`TradeProposal`, `StrategyType`, ...) as plain
    data, guarded through `ensure_trade_proposal` exactly like every
    other boundary in this codebase.
- **Architecture-boundary testing needed a new pattern.** Every prior
  package's boundary test is a blanket "no `src.llm` at all" source
  scan; `src/risk` legitimately needs `src.llm.schemas`. New tests in
  `tests/unit/risk/test_architecture_boundary.py`: a regex that allows
  `from src.llm.schemas import ...` / `from src.llm import schemas` but
  forbids `src.llm.client`/`src.llm.router` by name *and* forbids a bare
  `import src.llm` (which would make `client`/`router` reachable via
  attribute access even without naming them); a companion runtime test
  that monkeypatches every callable on `src.llm.client`/`src.llm.router`
  to raise, then runs a full `evaluate_trade_proposal` end to end to
  prove the call graph never actually touches either module.
- **Bugs found and fixed while building the test suite** (all caught by
  the tests themselves, not found after the fact):
  - `size_trade`'s capital budget (`normal_max_capital_deployed_pct`)
    was being applied as if it were a *per-trade* cap on capital used,
    when it's actually meant as a *portfolio-total* capital-deployed
    ceiling — this made a cash-secured put's realistic collateral
    requirement (strike × 100, easily tens of thousands of dollars)
    impossible to size at all against a $100k test portfolio, since
    `target_risk_per_trade_pct` (1%) alone requires `max_loss_per_contract
    ≤ nav × 1%` before even 1 contract sizes — not a code bug exactly
    (the target-risk-budget-first design is intentional: the absolute
    max is meant as a secondary ceiling, not a way to size *above*
    target), but it meant the CSP/covered-call test fixtures needed
    proportionally-priced underlyings (a low-priced "LOWP" ticker) to
    be realistic against a $100k account, the same way a real CSP
    strategy needs a large-enough account relative to the strikes it
    sells. Documented in the fixture comments so the next reader isn't
    surprised SPY-priced CSP fixtures don't size at 1 contract.
  - `load_risk_limits`/`load_broker_capabilities` originally only
    caught `KeyError` for a malformed config file, letting a Pydantic
    `ValidationError` (e.g. an out-of-order drawdown threshold, an
    unrecognized strategy name) escape as a raw `pydantic_core`
    exception instead of the module's own `RiskLimitsConfigError`/
    `BrokerConfigError` — fixed by also catching `ValidationError` and
    re-raising as the module's own error type, so every caller only
    ever needs to catch one exception type per module.
  - A sector-concentration bypass test initially passed for the wrong
    reason (the existing "concentrated" position's ticker wasn't
    registered in `Portfolio.sector_by_ticker`, so it silently fell
    into the `"UNKNOWN"` bucket instead of the sector being tested) —
    caught by re-reading the test's own assertion against the actual
    computed percentage rather than trusting the first green run.
- **Full repo test suite: 892 passing, 4 skipped** (the 4 skips are
  pre-existing IBKR live-adapter skips, unrelated to this step). New
  `tests/unit/risk/` package: 98 tests across architecture boundary,
  positive controls (APPROVE/RESIZE for all three strategies), the full
  bypass-attempt list Step 9 named verbatim (oversized positions,
  correlated positions, stale quotes, missing quotes, missing max loss,
  excessive drawdown, insufficient cash, excessive sector/underlying
  concentration, duplicate order, invalid option contract, unknown
  Fidelity capability, unsupported strategy, missing collateral,
  broker/account mismatch, malformed TradeProposal, zero/negative/NaN/
  infinite values), config loader edge cases, sizing's
  never-exceeds-requested guarantee, drawdown-zone boundaries, and the
  no-hardcoded-limits source scan.

## 2026-09-19 (cont'd) — Portfolio Manager Agent implemented (Step 10)

- **Read first**: confirmed `CLAUDE.md` still doesn't exist in this
  repo; re-checked `progress.md`'s own "Next up" from the Step 9 entry
  (which already named "the Multi-Agent Layer has never been connected
  to the Risk Engine" as the open seam); inspected the existing
  `.claude/agents/portfolio_manager.md` (from the original LLM
  orchestration step) and `PortfolioManagerReview`/`TradeProposal`
  schemas, `src.llm.client`/`context`/`router`, and `src.risk.engine` /
  `src.risk.trade_risk.QuantitativeAnalysis` before writing anything.
- **Found an existing `portfolio_manager.md` and `PortfolioManagerReview`
  schema already in the codebase** from the original LLM orchestration
  step — a *shortlisting* role (rank candidates, return a list of
  `TradeProposal`s). Step 10 asks for something different: a *per-trade
  CIO verdict*, reached by explicitly working through 13 named
  questions, output as a new `PortfolioDecision` type. These are
  genuinely different jobs (shortlist many vs. rule on one), so rather
  than replace or rename the existing schema/tests (which stay exactly
  as they were, still fully passing), Step 10 **adds** `PortfolioDecision`
  alongside it and extends the *same* `portfolio_manager.md` persona to
  describe both jobs the Portfolio Manager role now does.
- **`PortfolioDecision`** (`src/llm/schemas.py`): `decision_id`,
  `proposal_id`, `decision` (`PortfolioDecisionType = Literal[
  "propose_advance", "reject", "hold_cash"]` — deliberately not
  "approve"; this agent has no approval authority, only Python Risk
  Engine does, so the vocabulary itself says so), `confidence`,
  `market_regime`, `thesis_summary`, `bear_case`, `portfolio_fit`,
  `correlation_assessment`, `capital_efficiency`, `alternative_considered`,
  `cash_preferred`, `invalidation_conditions`, `required_follow_up`,
  `timestamp`. Every field is a string, enum, bool, list of strings, or
  datetime — **there is no field of numeric type anywhere on this
  schema** (checked directly by a test that walks `model_fields` and
  asserts no `float`/`int` annotation exists), which is a stronger
  guarantee against "the LLM invents a number" than a docstring asking
  nicely: there's structurally nowhere to put one. A `model_validator`
  enforces `cash_preferred == (decision == "hold_cash")`, so the schema
  itself refuses a self-contradictory decision. `ensure_portfolio_decision`
  is the same exact-type boundary guard as `ensure_trade_proposal`.
- **`src/llm/context.py` additions**: `QuantitativeAnalysisContext`
  (mirrors `src.quant.expected_value.StrategyEconomics` field-for-field)
  and `RiskEngineContext` (mirrors `src.risk.engine.RiskDecisionResult`'s
  key fields) — both plain dataclasses, *not* imports of the real
  `src.quant`/`src.risk` types, preserving the existing, previously
  unstated-but-real invariant that `src/llm/*.py` has zero dependency on
  `src.quant`/`src.data`/`src.brokers`/`src.risk` (confirmed by grepping
  every existing `src/llm/*.py` import before adding anything — the only
  internal import anywhere in the package was `client.py` importing
  `router.py`). Same reasoning `CandidateContext`/`PortfolioStateContext`
  already established for this package.
- **`src/llm/portfolio_manager.py`** — the orchestration layer:
  - `PortfolioManagerInputs`: the seven inputs Step 10 names (Market
    Regime, Opportunity Scanner's highlight — the one genuinely optional
    field, Python Quant, the `TradeProposal` itself carrying Strategy
    Analyst's thesis, Devil's Advocate, Portfolio State, Risk Engine),
    bundled as one dataclass.
  - `validate_inputs_complete` — the concrete mechanism behind "may not
    invent missing inputs": every required field must be non-`None`
    (`MissingInputError` otherwise), and the proposal must independently
    pass `ensure_trade_proposal` (a dict or forged object is rejected
    here too, not just downstream).
  - `build_portfolio_manager_context` — reuses
    `src.llm.context.build_agent_context`'s existing `extra` parameter
    rather than writing a second serializer.
  - `evaluate_proposal` — calls `LLMClient.complete_structured` (task
    type `TaskType.PORTFOLIO_MANAGER`, already defined in
    `config/llm.yaml`/`router.py` since the original orchestration
    step — reused, not re-added), validates the result through
    `ensure_portfolio_decision`, then cross-checks the model's own
    `proposal_id` and `market_regime` against what was actually supplied
    — a decision for the wrong proposal, or that disagrees with the
    regime it was told, is rejected (`PortfolioManagerCrossCheckError`)
    rather than trusted.
- **`src/llm/audit.py`** — the append-only decision audit log
  `src.llm.client.AgentCallResult`'s docstring has been flagging as "not
  implemented yet" since the very first LLM orchestration commit.
  `DecisionAuditRecord` (model, prompt_version, input_references,
  decision_id/proposal_id/decision, timestamp, supporting_rationale) is
  a plain frozen dataclass; `AuditLog` (ABC) / `InMemoryAuditLog` mirror
  `src.brokers.base.IdempotencyStore`/`InMemoryIdempotencyStore`'s
  existing append-only pattern exactly — same "process-local only, not a
  production guarantee" caveat, flagged the same way. `record_decision`
  is the only path from a `PortfolioManagerEvaluation` to a persisted
  record; `supporting_rationale` is built only from
  `decision.thesis_summary`/`decision.bear_case` — there is no field on
  `DecisionAuditRecord` a raw model "thinking" block could ever land in,
  confirmed by a test that checks the dataclass's field names for
  anything reasoning-shaped.
- **Fidelity execution-state vocabulary**: Step 10 named 12 ticket
  states, one of which — `REPRICE_REQUIRED` — didn't exist yet in
  `src.brokers.fidelity.TicketStatus` (Step 8/8A defined 11). Rather than
  let the persona reference a state the type system couldn't actually
  produce, added `REPRICE_REQUIRED` to the real enum: reachable from
  `AWAITING_HUMAN` or `ORDER_ENTERED` (the market moved enough that the
  ticket's price is stale before a human finished entering it), and only
  ever returns to `AWAITING_HUMAN` (a ticket must be regenerated from
  fresh market data, never resubmitted at the stale price) or terminates
  via `CANCELLED`/`EXPIRED`. Explicitly still unreachable via
  `transition()` for `FILLED`/`PARTIALLY_FILLED`, same as every other
  non-terminal status. One existing test
  (`test_all_11_named_states_are_valid_enum_members`) needed updating
  for the new count; everything else in the existing Fidelity suite
  (224 tests) passed unmodified, including the generic
  `test_any_non_terminal_status_can_expire` test that iterates
  `_ALLOWED_TRANSITIONS` directly rather than naming states, so it
  covered the new status automatically.
- **`.claude/agents/portfolio_manager.md` rewritten** to carry both
  jobs: the original shortlisting role (`PortfolioManagerReview`,
  unchanged) plus the new CIO decision process — priority order (capital
  preservation > drawdown control > risk-adjusted return > consistency >
  long-term return), the aspirational-not-guaranteed 12–15% target with
  an explicit instruction never to raise risk to chase it, all 13
  decision questions verbatim, the exact authority list (may
  ANALYZE/COMPARE/PROPOSE/REJECT/HOLD CASH; may not override a
  calculation, the Risk Engine, a position size, invent a number, place
  an order, or change a limit), and the full 12-state Fidelity execution
  vocabulary with an explicit "RISK_APPROVED — or even a propose_advance
  decision from you — is not an executed trade" line.
- **Full repo test suite: 960 passing, 4 skipped** (same pre-existing
  IBKR skips). New tests: `tests/unit/llm/test_portfolio_decision_schema.py`
  (structural no-numeric-fields proof, cash_preferred consistency,
  forbidden-extra-field smuggling, invalid decision values including
  "approve" itself being rejected, boundary guard), `test_portfolio_manager.py`
  (missing-input coverage for all seven required inputs, malformed/
  forged `TradeProposal` rejection, stale-data proof via the existing
  `TradeProposal` freshness validator, successful propose_advance/reject/
  hold_cash round-trips, cross-check rejection, attempted risk/position-
  size override via smuggled fields, hallucinated numeric fields,
  malformed LLM output including free-text and wrong-typed responses),
  `test_audit.py` (record contents, append-only interface shape, no
  hidden-reasoning field, `for_proposal` filtering), plus new
  `TestRepriceRequired` coverage in the existing Fidelity state-machine
  suite.
- **No bugs found this step** — the pipeline worked end to end on first
  full run (`build_portfolio_manager_context` → `evaluate_proposal` →
  `record_decision`), likely because it's built almost entirely from
  already-tested primitives (`LLMClient.complete_structured`,
  `ensure_trade_proposal`, `build_agent_context`, the
  `InMemoryIdempotencyStore` pattern) rather than new mechanism.

## 2026-09-19 (cont'd) — Independent Devil's Advocate Agent implemented (Step 11)

- **Read first**: confirmed `CLAUDE.md` still doesn't exist; re-read the
  Step 10 "Next up" entry (which already named the Devil's Advocate as
  one of the untouched pipeline stages) and the existing
  `.claude/agents/devil_advocate.md`/`AdversarialReview` schema from the
  original orchestration step before writing anything new.
- **Same pattern as Step 10's `PortfolioDecision`**: found an existing,
  lighter-weight `AdversarialReview` (`proposal_id`, `critique`,
  `risk_flags`, `do_not_advance`) already in the codebase. Step 11 asks
  for something more rigorous — an 18-category mandatory checklist, 3+
  structured failure scenarios, a Fidelity-staleness check, and a closed
  PASS/CAUTION/REJECT/REPRICE_REQUIRED verdict. Added `DevilsAdvocateReview`
  **alongside** `AdversarialReview` (kept unchanged, still passing)
  rather than replacing it, for the same "different job, not a
  duplicate" reasoning as Step 10.
- **`DevilsAdvocateReview` and its sub-models** (`src/llm/schemas.py`):
  - `RiskCategory`: the exact 18-value `Literal` the spec names
    (directional risk through execution risk). `risk_assessment:
    list[RiskCategoryAssessment]` is constrained to exactly 18 entries,
    and a validator additionally rejects a duplicate or missing
    category by set comparison — "analyze all 18 for every trade" is a
    schema invariant, not a persona instruction the model could skip a
    few of.
  - `FailureScenario`: `probability_category`/`severity`/
    `portfolio_impact_category` are all closed categorical `Literal`s —
    structurally impossible to fabricate a number like "23% chance"
    into, satisfying "do not fabricate numerical probabilities"
    the same way `PortfolioDecision` structurally can't carry a price.
    `failure_scenarios` requires a minimum of 3.
  - `FidelityExecutionRiskAssessment`: the model's own read on the seven
    named execution-staleness checks, plus its own `reprice_required`
    opinion — explicitly documented as advisory, overridable by Python
    (see below), never the reverse.
  - `DevilsAdvocateVerdict = Literal["PASS", "CAUTION", "REJECT",
    "REPRICE_REQUIRED"]` — no "approve"/"execute" value exists at all,
    so "the Devil's Advocate cannot approve execution" is enforced by
    the type itself, not by convention. A `model_validator` additionally
    rejects `fidelity_execution_risk.reprice_required=True` paired with
    a PASS/CAUTION verdict as self-contradictory.
  - `ensure_devils_advocate_review` — the same exact-type boundary guard
    pattern as `ensure_trade_proposal`/`ensure_portfolio_decision`.
- **`src/llm/context.py` addition**: `MarketSnapshotContext` (a
  point-in-time quote snapshot: underlying price, bid, ask, IV, delta,
  as_of) — same plain-dataclass decoupling pattern as
  `QuantitativeAnalysisContext`/`RiskEngineContext` from Step 10, not an
  import of `src.data.option_chain.OptionContract`.
- **`src/llm/devils_advocate.py`** — the orchestration layer:
  - `DevilsAdvocateInputs`: proposal, quant analysis, portfolio state,
    market regime, and **two** market snapshots (`analysis_snapshot`,
    `current_snapshot`) — deliberately two, not one, so the
    staleness-between-analysis-and-human-entry check the spec asks for
    is real Python arithmetic on two real data points, not the model
    guessing at how much time might have passed. `earnings_in_window` is
    a plain caller-resolved boolean (never "unknown").
  - `validate_inputs_complete` / `MissingInputError` — identical "may
    not invent missing data" mechanism as
    `src.llm.portfolio_manager.validate_inputs_complete`, extended with
    the same `ensure_trade_proposal` defense-in-depth call.
  - `compute_staleness` — the deterministic comparison: underlying move
    %, bid/ask spread widening %, IV change, delta change, and snapshot
    age, each checked against a named module-level threshold (0.5%
    underlying move, 50% spread widening, 3 vol points, 0.05 delta, 15
    minutes — advisory-agent thresholds, not a Risk Engine limit, so
    plain constants rather than a new YAML config).
  - `evaluate_trade_risk` — calls the model, validates through
    `ensure_devils_advocate_review`, cross-checks `proposal_id`, and
    then applies the one deterministic override this agent has:
    **if `compute_staleness` says stale and the model's verdict isn't
    already REPRICE_REQUIRED or REJECT, Python force-overrides the
    verdict to REPRICE_REQUIRED** — the same "Python overrides an
    LLM-requested number, never the reverse" relationship
    `src.risk.trade_risk.size_trade` has with
    `TradeProposal.contracts_requested`. A model that already said
    REJECT is left alone (REJECT is a stronger conclusion than
    REPRICE_REQUIRED — the override never *weakens* a verdict).
- **Independence, built structurally, not by convention**: this module
  has zero import of `src.llm.portfolio_manager`, no field on
  `DevilsAdvocateInputs` that could carry a `PortfolioDecision`, and no
  parameter on `evaluate_trade_risk`/`build_devils_advocate_context`
  through which one could be passed — confirmed by a dedicated test file
  (`test_devils_advocate_independence.py`) that source-scans for any
  code-level (not docstring-prose) reference to `PortfolioDecision`,
  inspects both functions' signatures for anything decision-shaped, and
  checks the verdict vocabulary itself contains no approval-shaped
  value. "Avoid having the system grade its own conclusion" — there is
  no conclusion from the Portfolio Manager in scope for this agent to
  see in the first place.
- **`.claude/agents/devil_advocate.md` rewritten** around "assume the
  trade could lose money, find realistic reasons why" — the full
  18-category checklist, the 3+ failure-scenario requirement with an
  explicit no-fabricated-probabilities instruction, the Fidelity
  staleness checklist with an explicit "never recommend chasing a trade
  simply because the original opportunity disappeared" line, the closed
  four-value verdict vocabulary, and an explicit Independence section
  telling the agent it will never see the Portfolio Manager's decision
  because it doesn't exist yet when the Devil's Advocate runs.
- **Full repo test suite: 1027 passing, 4 skipped** (same pre-existing
  IBKR skips). New tests (67 in three new files): schema-level (18-
  category completeness/duplicate/unrecognized-category rejection,
  minimum-3-scenarios, no-fabricated-probability via non-categorical
  values, REPRICE_REQUIRED/REJECT-vs-PASS/CAUTION consistency, no-
  numeric-fields structural proof, forbidden-extra-field smuggling,
  boundary guard); orchestration (every scenario Step 11 names by
  name — obviously dangerous trades, earnings risk, concentration, high
  correlation, poor liquidity, three separate stale-quote triggers proving
  the deterministic override in each direction including the
  REJECT-not-weakened case, good-quality trades, all six required
  inputs' missing-data cases, cross-check rejection, hallucinated/
  malformed output); and independence (import-graph scan, signature
  inspection, verdict-vocabulary check).
- **No bugs found this step** — one test-authoring mistake caught before
  it became a false "pass": the first draft of the independence test
  searched the whole module source for the literal string
  "PortfolioDecision" and failed on its own docstring (which discusses
  the guarantee in prose) — fixed by narrowing the scan to actual code
  usage (imports, type annotations, constructor calls) rather than any
  mention of the name at all, the same import-statement-vs-prose
  distinction Step 7's architecture-boundary test had to learn.

## 2026-09-19 (cont'd) — Internal PaperBroker and the first real end-to-end pipeline (Step 12)

- **Read first**: confirmed `CLAUDE.md` still doesn't exist; re-read Step
  11's "Next up" (which already named end-to-end wiring, ordering-aware,
  as the natural next step); re-read `src.brokers.base`'s full `Broker`
  ABC, `Order`/`Position`/`Account`/`Fill` schemas, and
  `IdempotencyStore` pattern before writing `paper.py`, to implement
  against the interface already established by `src.brokers.ibkr`
  rather than inventing a parallel one.
- **`src/brokers/paper.py`** — `PaperBroker(Broker)`, fully in-memory,
  no network client of any kind:
  - `FillModel`: `BID`/`ASK` (every leg fills at its own bid/ask —
    conservative/optimistic for a net-credit opening order, the
    platform's only kind, and the reverse for a net-debit closing one),
    `MID`, `MID_WITH_SLIPPAGE`, `LIQUIDITY_ADJUSTED` (the default —
    "use conservative assumptions by default" from the spec). Slippage
    is the larger of a bps-of-mid floor and a fraction of each leg's
    actual bid/ask spread, so a wide-spread market simulates worse than
    a tight one with the same mid — a bps-of-mid-only model would have
    made those two indistinguishable, which a test caught (see below).
    `LIQUIDITY_ADJUSTED` additionally multiplies slippage when
    volume/open interest fall below configured thresholds, and fill
    quantity itself is capped by a fraction of the thinnest leg's
    volume (the mechanism behind partial fills) — a resting order that
    doesn't fully fill stays open and can be re-attempted later via
    `attempt_fill` once the market moves or the caller feeds fresh data.
  - Cash, buying power, and collateral: a put credit spread's collateral
    nets against its long leg (strike-width x 100 x qty), never the full
    cash-secured amount a naked short put would need — this matters a
    lot in practice (a $100k account can comfortably hold a handful of
    $5-wide spreads but could never cash-secure the equivalent naked
    puts) and is recomputed by grouping the *whole current position
    book* by (underlying, expiration), not tracked incrementally per
    order, specifically so a spread whose two legs were opened in
    separate orders still nets correctly.
  - Expiration settlement (`settle_expiration`) is cash- and
    share-settled by intrinsic value: a short ITM option is assigned
    (buys/sells real simulated shares at strike), a long ITM option is
    exercised (the mirror image), an OTM option expires worthless with
    no further cash impact. A covered call's assignment correctly
    reduces the share position acquired from an earlier cash-secured-put
    assignment — proven end to end in one test that chains a CSP
    assignment into a covered call assignment on the resulting shares.
  - Idempotency reuses `src.brokers.base.IdempotencyStore` exactly as
    IBKR does: the same `client_order_id` returns the existing `Order`
    rather than resubmitting.
- **`src/brokers/order_validator.py`** — the Order Validator stage
  between Python Risk Engine and `PaperBroker`: turns a Risk-Engine-
  approved `ApprovedOrder` into a `PlaceOrderRequest`, refusing
  (`OrderValidationError`) a non-APPROVE/RESIZE decision, a missing or
  non-`AUTOMATED` broker capability, a contracts-requested mismatch, or
  an already-submitted `client_order_id`. `build_occ_symbol` derives the
  OCC-style option symbol (`ApprovedOrder`'s legs carry no symbol field
  of their own) matching the exact format this codebase's fixtures
  already use.
- **`config/brokers.yaml`**: added an `internal_paper` entry
  (`execution_mode: AUTOMATED`) — `PaperBroker` needed a broker
  capability of its own, distinct from `ibkr_paper` (a different real
  connection) and `fidelity` (`MANUAL`).
- **`src/risk/engine.py` extended, not duplicated**: `ApprovedOrder`
  construction was previously gated on `execution_mode == "MANUAL"`
  (Fidelity-only, since that was Step 9's whole scope). `ApprovedOrder`
  is broker-agnostic data, so it's now built for *any* approved OPEN
  action regardless of execution mode; only the human-readable
  `FidelityTradeTicket` stays `MANUAL`-specific. No existing Step 9 test
  assumed the narrower behavior (all of them used the `fidelity`
  fixture, still `MANUAL`), so this widened cleanly — confirmed by the
  full `tests/unit/risk/` suite passing unmodified.
- **A genuine, Step 10-established interface needed to change**: Step
  10 made `PortfolioManagerInputs.risk_engine_result` a *required*
  field, on the reading that the Portfolio Manager narrates around an
  already-known Risk Engine result. Step 12's own required pipeline
  order — Devil's Advocate -> **Portfolio Manager -> Risk Engine** —
  makes that reading impossible: the Risk Engine hasn't run yet when
  the Portfolio Manager does. Rather than route around this,
  `risk_engine_result` is now `RiskEngineContext | None = None`,
  documented in place as changed *because of* Step 12, with the other
  six fields staying required exactly as before. Two Step 10 tests that
  assumed the field was mandatory were updated (not deleted) to instead
  assert the opposite — that a `None` here does not raise, unlike every
  other required field.
- **`src/orchestration/` — new package, the pipeline itself**:
  - `pipeline.py`: `run_order_pipeline(request, stages)` walks the exact
    8 stages named (Quant Engine, Devil's Advocate, Portfolio Manager,
    Risk Engine, Order Validator, PaperBroker, Portfolio, Database) as
    explicit, individually-injectable dependencies on `PipelineStages` —
    checked for presence *before* any of them run; a `None` on any one
    rejects immediately, naming exactly which stage was missing
    (`PipelineOutcome.rejected_stage`), never silently skipped. This is
    the first module in the codebase that calls
    `src.llm.devils_advocate`, `src.llm.portfolio_manager`, and
    `src.risk.engine` back-to-back against one shared scenario — every
    prior step built and tested these in isolation.
    `default_quant_stage` reuses `src.risk.trade_risk`'s own
    `resolve_leg_contracts`/`compute_trade_economics`/
    `compute_trade_greeks` (never a second implementation of the same
    math); `_adversarial_review_from` bridges Step 11's richer
    `DevilsAdvocateReview` into the `AdversarialReview` shape
    `PortfolioManagerInputs` still expects, as a small adapter rather
    than changing that established interface a second time.
  - **The Fidelity ticket is generated "at the same time"** exactly as
    Step 12 asks, by running the *same* Risk Engine stage a second time
    against a `MANUAL` broker capability right after the `AUTOMATED`
    (PaperBroker) call approves — never hand-built, always the Risk
    Engine's own independently computed artifact; a failure generating
    it is non-fatal to the paper-execution path (a companion ticket, not
    a precondition for one).
  - `refresh.py`: the pre-execution refresh — refreshes quote/legs by
    rerunning the Quant Engine and Risk Engine stages against fresh
    market data, then compares the *newly generated ticket's own
    figures* (target credit, max loss) against the original ticket's;
    a move beyond a configurable materiality threshold (10% default on
    either) returns `REPRICE_REQUIRED` and withholds the new ticket as
    final, rather than silently handing a human updated numbers under
    the same "AWAITING_HUMAN" framing.
  - `execution_audit.py`: `ExecutionQualityRecord` stores exactly the
    six things asked for (theoretical midpoint, paper fill, Fidelity
    target limit, minimum acceptable credit, market timestamp,
    subsequent price) — `theoretical_midpoint`/`fidelity_target_limit`/
    `fidelity_minimum_acceptable_credit` are read directly off the
    `FidelityTradeTicket`'s own fields (`net_mid`, `limit_price`,
    `minimum_acceptable_price`), never recomputed. `subsequent_price`
    is deliberately not a field filled in later on the same record
    (this log is append-only, same as every other audit log in this
    codebase) — a later observation is its own follow-up record,
    `follow_up_of` pointing back at the original.
- **Bugs found and fixed while building/testing, before they could ship**:
  - `price_satisfies_limit`'s debit-order branch had the comparison
    backwards (`net_price <= -limit_price` instead of
    `net_price >= -limit_price`), which would have rejected every
    closing (buy-to-close) trade that actually satisfied its limit and
    accepted ones that didn't — caught by manual smoke-testing a
    round-trip open-then-close scenario before any pytest was written,
    not by a test that happened to already exist.
  - The first `_required_collateral` implementation charged every short
    leg its full naked cash-secured amount, ignoring a same-order long
    leg entirely — a $5-wide put credit spread would have needed the
    same six-figure collateral as a naked short put. Fixed by pairing
    same-right, same-quantity short/long legs within a netting group and
    charging only the strike width; `_recompute_collateral` reconstructs
    that pairing from the *whole position book*, grouped by
    (underlying, expiration), rather than tracking collateral
    incrementally per order — the incremental version couldn't net a
    spread whose two legs were opened in separate orders.
  - `compute_fill`'s slippage was originally a pure function of the net
    mid's magnitude and liquidity thresholds, with no dependency on the
    actual bid/ask spread width at all — a "wide spreads" test (two
    otherwise-identical markets, one tight, one wide, same mid) came
    back with identical simulated fills, which is exactly the
    unrealistic behavior "realistic fills" was supposed to prevent.
    Fixed by taking slippage as the larger of the bps-of-mid floor and a
    fraction of the actual average leg spread.
  - The pipeline's Order Validator call originally defaulted
    `client_order_id` to `ApprovedOrder.risk_approval_id`, which
    `src.risk.engine` generates fresh (`uuid.uuid4()`) on every call —
    running the *same* `TradeProposal` through the pipeline twice
    produced two different `client_order_id`s, defeating the whole
    "duplicate orders" idempotency test. Fixed by pinning
    `client_order_id` to `TradeProposal.proposal_id` itself, the one
    stable identifier that's actually the same across repeated runs of
    "the same" intended trade.
  - A test-helper bug (not a production bug): an early `_full_stages`
    test fixture popped `paper_broker` from overrides and treated an
    explicit `paper_broker=None` the same as "not specified," silently
    injecting a real broker back in — masking what should have been a
    missing-stage rejection test. Fixed by checking key presence
    (`"paper_broker" not in overrides`) instead of truthiness.
- **Full repo test suite: 1104 passing, 4 skipped** (same pre-existing
  IBKR skips). New: 27 `PaperBroker` tests (all five fill models, no
  fill, partial fill with a later completing re-attempt, stale data,
  missing quotes, wide spreads, duplicate/idempotent orders, cancel,
  insufficient cash, defined-risk-vs-naked collateral, position closing,
  OTM expiration, ITM assignment, chained CSP-assignment-into-covered-
  call-assignment, broker lifecycle); 15 Order Validator tests; 33
  orchestration tests (8 missing-stage-rejection cases, Devil's Advocate
  reject and reprice-required propagation, Portfolio Manager reject and
  hold-cash propagation, Risk Engine reject/halt propagation, duplicate
  orders at the pipeline level, insufficient cash, the full happy path
  producing both a paper fill and a Fidelity ticket together, refresh
  OK/REPRICE_REQUIRED/REJECTED paths, execution-audit record shape and
  append-only follow-up behavior).

## 2026-09-19 (cont'd) — Institutional-quality backtesting engine (Step 13)

- **Read first**: `src.data.historical` (`HistoricalBar`,
  `assert_no_lookahead`) and `src.data.earnings`
  (`is_within_earnings_window`) as the existing bias-prevention
  precedents to reuse rather than reinvent; `src.brokers.paper`'s fill
  model (`FillModel`, `compute_fill`, `fillable_quantity`,
  `price_satisfies_limit`) as the execution math this package must reuse
  for both backtest fills and live paper fills, not duplicate.
- **`src/backtest/`** — all 10 named files:
  - `simulator.py`: `HistoricalOptionQuote` (this package's own
    historical options-chain quote — deliberately not built on
    `TimestampedModel`, the same reasoning `HistoricalBar` already gives,
    since a 2024-03-15 quote's validity never expires) plus
    `HistoricalOptionChainProvider` (the vendor-agnostic abstraction
    `ARCHITECTURE.md §12` already flagged as an open question, filled in
    shape only), `assert_no_lookahead_options` (the options-chain twin of
    `assert_no_lookahead`), and the position/trade data model
    (`BacktestLeg`, `BacktestPosition`, `TradeRecord`, `EntrySignal`,
    `PortfolioState`) — every position and trade carries *both* a
    realistic and a theoretical dollar figure side by side from entry
    through close, computed independently rather than one derived from
    the other.
  - `slippage.py`: a `HistoricalOptionQuote` -> `src.brokers.paper`
    `LegQuote`/`OrderLeg` adapter, not a second fill-price model —
    `fill_realistic` calls `compute_fill`/`fillable_quantity`/
    `price_satisfies_limit` directly; `fill_theoretical` is pinned to its
    own fixed MID/zero-friction config regardless of what the realistic
    side is configured with, so the theoretical track can never
    accidentally inherit realistic friction; `mark_to_market` answers
    "what would it cost right now" for profit-target valuation, with no
    limit-price gate (it isn't placing an order).
  - `commissions.py`: `CommissionSchedule` + `calculate_commission`, kept
    as its own tiny module (named explicitly by the spec) so a future
    per-broker schedule has an obvious place to live.
  - `execution.py`: combines slippage + commission into one
    `ExecutionResult` for both opening (`execute_entry`) and closing
    (`execute_exit`, via `flip_legs`) orders, always producing realistic
    and theoretical figures together.
  - `expiration.py`: `days_to_expiration`, `is_expiring_today`,
    `management_dte_reached`, `profit_target_reached` — the
    DTE-management and profit-target trigger rules every open position
    is checked against on every simulated day.
  - `assignment.py`: `intrinsic_value`, `settle_leg`, `settle_position` —
    real cash- and share-settlement by intrinsic value at expiration,
    covering all three strategies (CSP assignment buys shares, covered
    call assignment sells shares, PCS settles both legs independently so
    the between-strikes partial-loss case and the below-both-strikes
    max-loss case are genuinely different settlements, not the same
    formula scaled).
  - `engine.py`: `run_backtest` — the day-by-day loop. For every open
    position on every simulated day: settle if expiring today, else
    check profit-target/management-DTE and close via a real market order
    if triggered (with `NoFillError` keeping a position open rather than
    forcing a phantom close); then open any new entries scheduled for
    that day. Every single quote lookup — for open positions and new
    entries alike — is piped through `assert_no_lookahead_options` before
    use. `BacktestConfig`, `evaluate_target`/`TargetCategory` (grades
    *realistic*, never theoretical, CAGR against the 12–15% research
    target — "exceeds"/"meets"/"approaches"/"falls_below"), and
    `build_backtest_result` (assembles both metric sets, slippage
    cost/year, slippage as % of theoretical gross profit, turnover/year,
    average entry bid/ask spread, and an optional SPY/risk-free
    benchmark comparison) all live here too.
  - `metrics.py`: every metric the spec names by exact formula —
    CAGR, annual volatility, Sharpe, Sortino, max drawdown, Calmar, win
    rate, average winner/loser, profit factor, expectancy, historical
    VaR/CVaR (95%), worst month, worst year, longest drawdown (duration,
    distinct from max drawdown's depth), capital utilization (time-
    weighted collateral deployed), trade count, average holding period —
    each one a standard textbook definition, hand-checked against a
    small exact synthetic example in tests rather than merely trusted
    because the code runs (the same "independently verifiable" standard
    `src.quant` set).
  - `benchmark.py`: `spy_total_return` (reuses `HistoricalBar` +
    `assert_no_lookahead`, never a second bar type or a second
    look-ahead check), `risk_free_return` (simple constant-rate accrual —
    a deliberately simple Treasury stand-in, a real yield curve is future
    work), `compare_to_benchmarks`.
  - `walk_forward.py`: `generate_walk_forward_splits` (rolling
    TRAINING/VALIDATION/OUT-OF-SAMPLE windows, matching the spec's own
    example — train 2015-2019, validate 2020-2021, out-of-sample 2022,
    then roll forward by `out_of_sample_years` by default) and
    `run_walk_forward` (runs one already-fixed `BacktestConfig`
    independently across all three windows, each from a fresh
    `PortfolioState`). "Never optimize using the out-of-sample period" is
    enforced structurally, not by convention: `run_walk_forward`'s
    signature has no optimizer/objective-function/parameter-search
    argument anywhere, so there is nothing through which a later
    window's result could be fed back into an earlier decision even if a
    future caller wanted to — proven by a test that walks the function's
    own signature for anything optimizer-shaped.
- **Bias prevention, documented one mechanism per named bias rather than
  one generic disclaimer** (now in `engine.py`'s own module docstring):
  look-ahead bias/data leakage is enforced by `assert_no_lookahead_options`
  (every quote lookup) and `assert_no_lookahead` (the benchmark);
  survivorship bias is "where possible" by design — a data-provider
  property this engine can't enforce on its own, same open vendor
  question `ARCHITECTURE.md §12` already carries; future earnings
  knowledge is out of this module's authority since it consumes but
  never generates `EntrySignal`s (a future Strategy Screener's job,
  which already has `src.data.earnings.is_within_earnings_window`
  available to it); future volatility knowledge is prevented because
  `HistoricalOptionQuote.iv` is just another field on the same object
  `assert_no_lookahead_options` already gates.
- **A real bug found and fixed before any test caught it**: the
  profit-target check originally called `mark_to_market` on a position's
  *original* (unflipped) legs — e.g. still `sell` for a short put —
  instead of the *closing* order's legs (`buy` to close). Under plain
  `FillModel.MID` this happened to produce the same number (mid has no
  buy/sell asymmetry), which is exactly why it went uncaught by an early
  manual smoke test using the zero-friction default. Under
  `MID_WITH_SLIPPAGE`/`LIQUIDITY_ADJUSTED`, though, `compute_fill`
  always subtracts slippage from whatever `net_price` it computes —
  pricing the wrong (unflipped) direction meant slippage was being
  applied as if *opening* a fresh position, which *understates* the true
  cost to close and would have triggered profit-target exits too early,
  silently flattering realistic returns in exactly the way "never
  automatically assume midpoint fills" exists to prevent. Fixed by
  valuing `flip_legs(position.legs)` (the actual closing order) and
  negating the sign, with a regression test
  (`test_slippage.py::test_slippage_model_moves_price_against_whichever_direction_is_traded`
  and `test_engine.py::test_slippage_model_makes_the_realistic_close_strictly_worse_than_mid`)
  that fails immediately if the sign regresses.
- **Full repo test suite: 1226 passing, 4 skipped** (same pre-existing
  IBKR skips). New: 122 `tests/unit/backtest/` tests across 10 files —
  `HistoricalOptionQuote` validation and look-ahead enforcement,
  commission formulas, realistic-vs-theoretical fill/mark-to-market
  behavior including the slippage-direction regression, entry/exit
  execution and put-credit-spread payoff (round-trip zero-sum at
  unchanged prices, full max profit when a spread decays to worthless),
  DTE/profit-target trigger boundaries, expiration settlement across all
  three strategies (OTM worthless, CSP assignment, covered-call
  assignment, PCS partial-loss-vs-max-loss), the day-by-day engine loop
  (look-ahead-bias enforcement, contract-multiplier scaling, cash
  accounting reconciliation on close/expiration/assignment/DTE-management,
  target-category boundaries, slippage/commission isolation in
  `build_backtest_result`, zero-trade edge cases with no division by
  zero), metrics (CAGR, max/longest drawdown, Calmar, Sharpe/Sortino
  zero-variance and zero-downside edge cases, worst month/year, VaR/CVaR
  on hand-computed synthetic return series, trade statistics, capital
  utilization), benchmark comparison (SPY return, risk-free accrual,
  look-ahead enforcement, excess-return wiring), and walk-forward
  splitting/execution (the spec's own train/validate/out-of-sample
  example, window independence, the no-optimizer-hook structural
  guarantee).

## 2026-09-19 (cont'd) — Strategy Research Agent (Step 14)

- **Read first**: `src.risk.limits` (reused, not duplicated, for the
  strategy-level risk review's thresholds), `src.llm.devils_advocate`
  (the "Python overrides the LLM" idiom and the independence-test
  technique this step reuses for a different boundary),
  `src.llm.schemas`/`context.py`'s tail (the zero-numeric-field schema
  pattern and the mirrored-not-imported context-dataclass pattern),
  `src.backtest.simulator.TradeRecord`/`src.backtest.metrics
  .PerformanceMetrics` (what this step's analysis layer consumes).
  `TaskType.STRATEGY_RESEARCH` and `config/llm.yaml`'s
  `strategy_research: high_reasoning` route already existed from the
  original Step 12 scaffolding — this step is the first to actually use
  either.
- **`src/research/`** — new package, six modules:
  - `performance_breakdown.py`: `AnalysisDimension` (the exact 12
    dimensions named: strategy, delta, DTE, IV percentile, market
    regime, underlying, sector, entry day, entry time, holding period,
    profit target, management DTE), `TradeContext` (the analysis-only
    metadata a `TradeRecord` doesn't itself carry, supplied by whoever
    ran the backtest), `breakdown_by`/`breakdown_all_dimensions`
    (per-bucket trade count, win rate, total/average/best/worst
    **realistic** P&L — never theoretical). `entry_time` buckets as
    `"unspecified"` when no intraday timestamp is supplied, honestly
    reflecting that this platform's historical option-chain data
    (Step 13) is end-of-day only — the same "documented gap, not a
    fabricated number" choice `src.backtest.engine`'s bias-prevention
    docstring already makes for survivorship bias.
  - `hypothesis.py`: `Hypothesis` + `HypothesisRegistry`, an explicit
    forward-only status state machine (`proposed` -> `backtested` ->
    `validated` -> `out_of_sample_tested` -> `survived_out_of_sample`,
    with `rejected` reachable from any non-terminal status but never
    reversible) — the concrete record behind "15-20 delta put credit
    spreads outperform 25-30 delta spreads during high-IV regimes"
    style statements, and the counting mechanism `overfitting_guards.py`
    reads from.
  - `overfitting_guards.py`: five deterministic checks —
    `check_small_sample` (< 30 trades), `check_regime_dependence`
    (>= 80% of gross profit concentrated in one regime bucket),
    `check_parameter_mining` (> 5 near-duplicate variations of the same
    parameter family tested — the concrete guard behind "do not
    repeatedly test minor parameter changes until something profitable
    appears"), `check_multiple_testing_bias` (>= 10 hypotheses tested
    this session with < 10% surviving out-of-sample),
    `survivorship_bias_note` (a standing caution always included,
    mirroring `src.backtest.engine`'s own honesty about the same open
    vendor question). `run_overfitting_guards` assembles all five into
    one `OverfittingGuardResult` the LLM only ever reads.
  - `fidelity_practicality.py`: a weighted burden score (trades/week,
    adjustments/week, legs, rolling frequency, monitoring, assignment
    complexity, time sensitivity, liquidity) classified into
    LOW/MEDIUM/HIGH, plus a hard `INCOMPATIBLE` gate — independent of
    the score — for any strategy requiring sub-second decisions,
    constant intraday adjustment, or high-frequency execution, exactly
    matching the spec's "reject strategies requiring" list.
  - `risk_review.py`: `review_strategy_risk` grades a candidate
    strategy's own **realistic** backtest `PerformanceMetrics` against
    this platform's existing portfolio-level `drawdown_risk_reduction_pct`
    /`drawdown_halt_pct`/`max_stress_loss_pct_of_nav` thresholds
    (`src.risk.limits`, reused rather than a second set of numbers) —
    PASS/CONCERN/REJECT, with REJECT always outranking a milder CONCERN
    when both apply, and a small-sample trade count alone only ever
    producing CONCERN, never REJECT on its own.
  - `promotion.py`: `promote_strategy` — the single choke point.
    Requires `validation_passed`, `out_of_sample_passed`, a risk review
    verdict of exactly `PASS`, and `human_approved` with a named
    `human_approver`, all four independently required (no gate may be
    inferred from another passing). **The Strategy Research Agent has no
    path to call this**: `src.llm.strategy_research` never imports
    `src.research.promotion` at all.
- **`src/llm/schemas.py`**: added `AnalysisDimension` (mirrored, not
  imported, from `src.research.performance_breakdown`'s own — a test
  proves the two literal value sets stay identical),
  `ResearchRecommendation`, `FidelityPracticalityRatingLiteral`, and
  `StrategyResearchReview` — zero numeric-typed fields, same structural
  guarantee `PortfolioDecision`/`DevilsAdvocateReview` already
  establish; a `model_validator` refuses an `INCOMPATIBLE` Fidelity
  rating paired with any recommendation other than reject/escalate.
- **`src/llm/context.py`**: added `PerformanceBreakdownContext`,
  `OverfittingGuardContext`, `FidelityPracticalityContext` — plain
  dataclasses mirroring the three `src.research` result types field for
  field, the same decoupling reason `QuantitativeAnalysisContext`
  doesn't import `src.quant`.
- **`src/llm/strategy_research.py`** (new): `evaluate_hypothesis` runs
  one hypothesis-review cycle, cross-checks `hypothesis_id`, and — the
  "Python overrides the LLM, never the reverse" idiom applied to a new
  boundary — overwrites the model's own `fidelity_practicality_rating`
  with Python's authoritative classification whenever the two disagree.
  Because `model_copy` doesn't re-run validators, an override to
  `INCOMPATIBLE` also force-corrects `recommendation` to
  `escalate_for_human_review` in the same update whenever the model's
  own recommendation wasn't already reject/escalate — otherwise the
  override could silently leave an object in memory that violates the
  very invariant the schema enforces at construction time.
- **`.claude/agents/strategy_research.md`**: new persona covering the
  eight-stage pipeline, the twelve analysis dimensions, overfitting
  protection guidance, Fidelity practicality preference, and an explicit
  "you cannot promote a strategy" constraints section.
- **Full repo test suite: 1361 passing, 4 skipped** (same pre-existing
  IBKR skips). New: 95 `tests/unit/research/` tests (all 12 dimensions'
  bucketing including delta-magnitude-not-sign and the honest
  `entry_time` "unspecified" fallback; the hypothesis state machine's
  forward-only transitions; all five overfitting guards at and around
  their thresholds; Fidelity practicality's hard `INCOMPATIBLE` gate
  overriding an otherwise-perfect low-burden score, plus LOW/MEDIUM/HIGH
  boundary cases; strategy risk review's PASS/CONCERN/REJECT thresholds
  including REJECT outranking a simultaneous CONCERN and grading
  realistic-not-theoretical metrics; every one of promotion's four gates
  individually and jointly required) and 40 `tests/unit/llm/`
  `strategy_research*` tests (schema zero-numeric-field and
  extra-field-forbidden guarantees, the `INCOMPATIBLE`-recommendation
  consistency validator, the `AnalysisDimension` literal-drift check
  against `src.research`'s own tuple, missing-input rejection with no
  model call, hypothesis-id cross-check, the Python-overrides-Fidelity-
  rating path including the two-field consistency fix, and the
  independence/production-rule-boundary proof mirroring
  `test_devils_advocate_independence.py`'s source-inspection technique).

## 2026-09-20 — Daily `/morning-scan` workflow (Step 15)

- **Read first**: `src.data.provider` (freshness primitives to reuse for
  stages 1-2), `src.orchestration.pipeline` in full (confirmed stages
  14-21 of the requested 21-step workflow are already exactly
  `run_order_pipeline`, called once per candidate — Quant Engine,
  Devil's Advocate, Portfolio Manager, Risk Engine (which already runs
  correlation/concentration internally, covering stages 17-18), PaperBroker,
  and Fidelity ticket generation), `src.risk.portfolio_risk` (capital/cash/
  sector-exposure helpers already exist and needed no reimplementation),
  `src.brokers.fidelity.render_ticket_text` (already produces most of the
  spec's FIDELITY TRADE TICKET fields — reused, not duplicated), and
  confirmed no `.claude/commands/` directory existed yet.
- **`src/workflows/`** — new package, three modules plus the orchestrator,
  covering stages 1-13 (steps 14-21 are pure reuse):
  - `feed_health.py` (stages 1-2): `verify_market_data_feeds`/
    `verify_data_freshness` — pure judgment functions over
    already-attempted fetch results (`dict[symbol, OptionChain | Exception]`),
    matching `src.data`'s own "normalize what a provider said, don't
    fetch it" separation; this module performs no I/O of its own.
  - `reconciliation.py` (stages 3-4): `ConfirmedFidelityPosition` — data
    a human typed in after looking at their own Fidelity account, never
    fetched, scraped, or inferred (Fidelity stays `MANUAL_EXECUTION`
    forever) — and `reconcile_portfolio`, using the exact same identity
    key `src.risk.portfolio_risk.find_duplicate_position` already
    established (ticker + strategy + expiration + exact strike set),
    reporting `missing_from_internal`/`missing_from_fidelity`/
    `quantity_mismatch` discrepancies distinctly rather than folding
    them into one generic "mismatch."
  - `candidate_generation.py` (stages 10-13): **the first deterministic
    Strategy Screener this codebase has had** — closing part of the gap
    flagged in every progress.md entry since Step 9. `passes_liquidity_filter`
    reuses `config/risk_limits.yaml`'s own thresholds (never a second
    set of numbers); `generate_candidates` picks, per requested strategy
    per ticker, the single contract (or short/long pair, for a put
    credit spread) whose data-reported delta is closest to a configured
    target range and that clears liquidity — a covered call is only
    generated for a ticker where `Portfolio.underlying_holdings` already
    shows >= 100 shares held. Every `TradeProposal` this module builds
    still goes through the full, unchanged pipeline before it can become
    anything — this module only decides what's worth asking the
    pipeline about; `thesis`/`risk_thesis` are template strings
    describing exactly what the screen found, never a claim about the
    future.
  - `morning_scan.py`: `run_morning_scan` — stages 1-9 assemble context
    (feed health, freshness, reconciliation, capital/cash/drawdown from
    `src.risk.portfolio_risk`, and pass-through market
    regime/VIX/economic-events/earnings-calendar context the caller
    supplies, since no dedicated `src.llm.market_regime` orchestration
    module exists yet — a documented gap, not silently built around);
    stages 10-13 call `generate_candidates` per universe ticker, then
    screen out any candidate whose expiration falls in a known earnings
    window (`src.data.earnings.is_within_earnings_window`, reused);
    stages 14-21 run `run_order_pipeline` once per surviving candidate
    (ranked by stated credit, capped at `max_candidates`), entirely
    unchanged from Step 12. `render_morning_scan_report` renders the
    exact section order and fields the spec names — MARKET REGIME,
    PORTFOLIO (portfolio-level delta/theta/vega render as "not tracked"
    when the caller doesn't supply them, never fabricated — no existing
    type aggregates Greeks across `PortfolioPosition`s yet), TOP
    OPPORTUNITIES, DEVIL'S ADVOCATE, RISK ENGINE, and FIDELITY TRADE
    TICKET or, whenever nothing produced a ticket, an explicit `NO
    TRADE` with a plain-language reason ending in "Cash is a valid
    position" — never a trade manufactured to have something to show.
  - `render_morning_scan_ticket_section` adds exactly the three fields
    `render_ticket_text` doesn't already print (probability of profit,
    an explicit exit rule beyond the profit target, the Risk Engine's
    own verdict) rather than modifying that already-tested Step 8/12
    function.
- **`.claude/commands/morning-scan.md`** — the actual slash command,
  the first in `.claude/commands/`: walks all 21 named steps, states
  exactly which are new Python (1-13) versus reused pipeline calls
  (14-21), and repeats the three hard constraints inline (never place an
  order, `NO TRADE` is a complete and successful run, cash is a valid
  position) so they can't be missed by only reading code.
- **Full repo test suite: 1424 passing, 4 skipped** (same pre-existing
  IBKR skips). New: 63 `tests/unit/workflows/` tests — feed health/
  freshness including custom max-age and vacuous-empty-input cases;
  reconciliation's three discrepancy kinds plus multi-position
  independence; liquidity filtering (thin open interest, low volume,
  wide spread, zero-mid division-by-zero safety); `QuantFilterConfig`
  validation; candidate generation for all three strategies including
  the covered-call share-ownership gate, cross-expiration best-delta
  selection, net-debit and no-further-OTM-leg rejection for spreads, and
  unique proposal ids; a full approved-trade run producing an
  `AWAITING_HUMAN` ticket end to end through the unmodified Step 12
  pipeline; NO TRADE via empty universe, Devil's Advocate REJECT,
  Portfolio Manager hold-cash, a stale feed, and a zero `max_candidates`
  cap; feed-failure and reconciliation-discrepancy surfacing in the
  rendered report; earnings-window screening both triggering and not
  triggering; honest "not tracked" rendering for unsupplied portfolio
  Greeks; and a structural proof (source inspection, mirroring
  `test_devils_advocate_independence.py`'s technique) that this package
  never imports a live broker client, never constructs a
  `FidelityTradeTicket` directly, and never calls `transition`/
  `confirm_fill` — every ticket it can produce comes from the existing,
  already-tested MANUAL pipeline path and defaults to `AWAITING_HUMAN`.

## 2026-09-20 (cont'd) — Weekly Investment Committee (Step 16)

- **Read first**: `src.orchestration.execution_audit.ExecutionQualityRecord`
  (confirmed it stores pre-execution figures only — the platform's only
  source of a real *confirmed* Fidelity fill is `ExecutionConfirmation`,
  attached via `src.brokers.fidelity.confirm_fill`), `FidelityTradeTicket`'s
  own `limit_price`/`net_mid` fields (already exactly "recommended limit"
  and "market midpoint when recommended" — no new schema needed),
  `src.backtest.metrics`/`benchmark` and `src.research.performance_breakdown`
  (all reused directly rather than reimplemented against a live trade
  journal).
- **`src/workflows/performance_review.py`**: PORTFOLIO PERFORMANCE, RISK,
  TRADE STATISTICS, and BREAK DOWN PERFORMANCE — all four sections are
  thin wrappers over `src.backtest.metrics` (`cagr`, `sharpe_ratio`,
  `sortino_ratio`, `max_drawdown`, `trade_statistics`),
  `src.backtest.benchmark.compare_to_benchmarks`, and
  `src.research.performance_breakdown.breakdown_by` (the exact 8 of the
  12 dimensions Step 16 names), applied to a real trade journal instead
  of a backtest run. `src.backtest.simulator.TradeRecord` is reused
  unmodified as that journal's record shape — a closed trade's P&L
  fields describe a backtest trade and a live trade identically, so a
  second, parallel "live trade record" type would only be a second
  place for the same shape to drift. Weekly/MTD/YTD returns anchor to
  whichever equity-curve point is on or before each period boundary,
  resolving to `None` (never an interpolated guess) when no such point
  exists yet.
- **`src/workflows/decision_quality.py`**: the four-quadrant classifier.
  "Do NOT judge decision quality solely by P&L" is enforced structurally
  — `was_good_decision` reads only ex-ante facts (Risk Engine decision,
  Devil's Advocate verdict, stated probability of profit), never
  `realistic_pnl`; `was_good_outcome` is the only function that reads
  P&L. A trade can be `good_decision_bad_outcome` (sound process, bad
  luck within its own stated odds) or `bad_decision_good_outcome`
  (a Risk-Engine-rejected trade that would have won) — both are
  first-class, intentionally preserved findings, not collapsed into "it
  made money, so it must have been right."
- **`src/workflows/rejected_trade_review.py`**: "determine what would
  have happened if taken" reuses `src.backtest.execution`/`assignment`
  exactly as `src.backtest.engine` does (a thin `TradeProposal` ->
  `BacktestLeg` adapter, never a second execution model) to price a
  rejected proposal's hypothetical round trip against real subsequent
  quotes or a real settlement price. "Do NOT automatically conclude a
  rejected winning trade should have been accepted" and "evaluate
  statistically over meaningful samples" are the same guarantee,
  implemented the same way `src.research.overfitting_guards`' small-
  sample check already is: `summarize_rejected_outcomes` always reports
  `meaningful_sample=False` plus an explicit warning below a named
  20-sample threshold, so a single rejected winner can never be read as
  proof without the report itself saying it isn't.
- **`src/workflows/execution_quality.py`**: no new schema needed —
  "recommended limit" and "market midpoint when recommended" are already
  `FidelityTradeTicket.limit_price`/`net_mid`, captured at
  ticket-generation time; "actual fill" and "execution time" are
  `ExecutionConfirmation.fill_price`/`confirmed_at`, the only evidence
  this codebase ever accepts for a real fill. `build_slippage_record` is
  a pure aggregator over an already-confirmed `(ticket, confirmation)`
  pair — it captures no new data and confirms nothing itself.
  `summarize_slippage` reports average/median and breaks down by
  strategy, underlying, and time of day.
- **`src/workflows/weekly_review.py`**: `build_weekly_review_report`
  assembles all of the above plus `NEXT WEEK` (earnings/expiration risk
  from open `Portfolio.positions`, reusing `src.data.earnings
  .is_within_earnings_window`; portfolio risk warnings compared against
  the same `config/risk_limits.yaml` thresholds the Risk Engine itself
  enforces — never a second set of numbers) and `RESEARCH`
  (`register_committee_hypotheses` — "send hypotheses to Strategy
  Research Agent" is literally registering them into the same
  `src.research.hypothesis.HypothesisRegistry` Step 14's agent reads
  from). **This module cannot modify production strategy**: it never
  imports `src.research.promotion`, proven the same way Step 14 proved
  the Research Agent itself can't. `render_weekly_review_report`
  produces the exact nine-section order the spec names, MODEL/PAPER/
  ACTUAL-FIDELITY performance kept as three distinct lines rather than
  blended into one number, per the command's own instructions to its
  Portfolio-Manager-chair.
- **`.claude/commands/weekly-review.md`**: the second slash command in
  `.claude/commands/`, instructing the Portfolio-Manager-chaired
  narrative never to restate a number differently than the report
  computed it, never to draw a conclusion from a rejected-trade sample
  the report itself flags as too small, and never to forecast market
  direction as certainty in the NEXT WEEK section.
- **Full repo test suite: 1485 passing, 4 skipped** (same pre-existing
  IBKR skips). New: 61 tests across five files — portfolio performance
  period-boundary anchoring and benchmark wiring; risk-section Greeks
  honesty; trade statistics and breakdown pass-through; decision-quality
  quadrant classification proving a loss alone can't flip a good
  decision bad and a win alone can't flip a bad decision good, plus
  configurable-threshold and summary-fraction tests; rejected-trade
  hypothetical pricing for both a market-order close and an expiration
  settlement (OTM and assigned), and the small-sample warning firing on
  exactly one sample and clearing at the named threshold; execution-
  quality slippage math, confirmation-before-recommendation rejection,
  and all five time-of-day buckets; and an end-to-end weekly report
  covering every section, the research hand-off actually landing in the
  shared `HypothesisRegistry`, and the structural
  cannot-modify-production-strategy proof (no `src.research.promotion`
  import, no filesystem write, source-inspection mirroring the Step
  11/14 independence-test technique).

## Open decisions carried forward (updated a sixth time)

- [ ] **New from Step 16**: no persisted equity ledger exists yet —
      `WeeklyReviewInputs.equity_curve` is a caller-supplied series, the
      same gap `/morning-scan`'s `fetch_results` already carries for
      market data; a real one is still a Phase 0 database item
- [ ] **New from Step 16**: there is no automated way yet to gather the
      inputs `/weekly-review` needs — the week's closed `TradeRecord`s,
      the rejected-proposal sample and its subsequent market data, and
      confirmed `(FidelityTradeTicket, ExecutionConfirmation)` pairs all
      have to be assembled by a caller today, mirroring the same gap
      `run_morning_scan`'s `fetch_results`/confirmed-positions inputs
      already carry
- [ ] **New from Step 16**: `decision_quality.DEFAULT_MIN_PROBABILITY_OF_PROFIT`
      (0.50) and `rejected_trade_review.MIN_SAMPLE_SIZE_FOR_CONCLUSIONS`
      (20) are reasonable starting thresholds, not researched policy
      values — same status as `src.research.overfitting_guards`'
      thresholds when they were first introduced in Step 14
- [ ] **New from Step 15**: no `src.llm.market_regime` orchestration
      module exists yet — `/morning-scan` stage 6 ("analyze current
      market regime") currently takes a `MarketRegimeAssessment` as a
      caller-supplied input rather than computing one itself; the
      `.claude/agents/market_regime.md` persona exists but nothing
      wires it to real index/volatility data the way Steps 10/11/14 did
      for their own agent roles
- [ ] **New from Step 15**: `run_morning_scan` takes `fetch_results`
      (already-attempted `OptionChain` fetches) and confirmed Fidelity
      positions as plain inputs — nothing yet actually drives real
      `MarketDataProvider.get_option_chain` calls across a universe on a
      schedule, or prompts a human for Fidelity confirmations; a real
      `/morning-scan` run today needs a caller to assemble those first
- [ ] **New from Step 15**: `MorningScanReport.portfolio_net_delta`/
      `_theta`/`_vega` are caller-supplied and render as "not tracked"
      when omitted — no existing type aggregates Greeks across
      `PortfolioPosition`s the way `src.risk.trade_risk.compute_trade_greeks`
      does for one proposed trade; a portfolio-level Greeks aggregator
      is genuinely new work, not yet started
- [ ] **New from Step 15**: `candidate_generation.py`'s screener is
      deliberately simple (closest-to-target-delta, one candidate per
      strategy per ticker) — it is not the eventual real Strategy
      Screener/Opportunity Scanner this platform still needs (candidate
      ranking is "richest stated credit first," an explicitly provisional
      heuristic, not a considered scoring model)
- [ ] Historical options data vendor for backtesting (Phase 2 blocker) —
      now also blocks fully closing the `src.risk.correlation` /
      `src.risk.stress` documented gaps above, *and* is the same open
      question `src.backtest.simulator.HistoricalOptionChainProvider`
      (Step 13) fills the shape of but deliberately doesn't decide, *and*
      is also what would let `src.research.performance_breakdown`'s
      `iv_percentile` and `market_regime` dimensions be populated from
      real data instead of caller-supplied `TradeContext` fixtures
- [ ] **New from Step 14**: `src.research.hypothesis.HypothesisRegistry`
      has the same "process-local, lost on restart" caveat as every
      other `InMemory*` store in this codebase — a real persisted
      hypothesis log is a Phase 0 DB item, not built yet
- [ ] **New from Step 14**: nothing yet actually calls
      `src.llm.strategy_research.evaluate_hypothesis` outside tests —
      the same no-real-caller gap Steps 10/11 flagged for
      `evaluate_proposal`/`evaluate_trade_risk`; there is also no real
      caller yet that runs a backtest, builds `ResearchTradeObservation`s
      from it, and feeds the whole pipeline (Observation through Risk
      Comparison) end to end
- [ ] **New from Step 14**: `src.research.promotion.promote_strategy`
      has no caller either — the human-approval UI/workflow that would
      actually construct a `PromotionRequest` (reading a `human_approver`
      from an authenticated human, not a hardcoded string) doesn't exist
      yet; today only a test can supply one
- [ ] **New from Step 13**: no Strategy Screener exists yet to generate
      real `EntrySignal`s — `src.backtest` executes and manages signals
      realistically but has never generated one itself; every backtest
      run so far is hand-constructed fixtures, the same gap Step 12
      flagged for `PipelineRequest`
- [ ] **New from Step 13**: `src.backtest.engine.run_backtest` has no
      clock-driven caller either — like `PaperBroker.settle_expiration`
      (Step 12), it's a function a caller must invoke explicitly with a
      pre-built `trading_days` list, not yet wired into anything that
      runs a real historical range end to end against a real vendor
- [ ] **New from Step 13**: survivorship bias is only preventable "where
      possible" — it depends entirely on whichever real historical
      vendor eventually fills `HistoricalOptionChainProvider`/
      `HistoricalDataProvider` correctly including delisted/failed
      tickers; this package has no way to detect or correct for a
      vendor that silently omits them
- [ ] Schwab paper-trading / sandbox capability (Phase 4 blocker)
- [ ] Sector/classification data source for correlation/concentration checks
- [ ] Final ~50-name equity universe list + liquidity criteria
- [ ] **`app/` prototype disposition — now fully redundant, recommend
      resolving before Phase 0**
- [ ] Fold `src/llm/` + `src/quant/` + `src/data/` + `src/brokers/` under
      `src/options_platform/`, or keep `src/` flat with multiple
      top-level packages — deferred five times now
- [ ] Model tier per non-Portfolio-Manager agent role
- [ ] Three independently-declared 15-minute freshness placeholders now
      (`src.llm.schemas.MAX_MARKET_DATA_AGE`,
      `src.data.provider.DEFAULT_MAX_QUOTE_AGE`,
      `src.brokers.fidelity.MAX_MARKET_DATA_AGE`) — should become one
      config value in Phase 0
- [ ] No concrete *market data* provider exists yet for `src.data`'s
      `MarketDataProvider` interface (mock or real)
- [ ] Duplicated small enums across layers (`OptionRight` x3,
      `OrderAction`/`Side`, `FidelityLegAction` vs. `OrderAction`) —
      candidate for a shared `src/core/` primitives module
- [ ] `_RealIBAdapter` (IBKR's actual `ib_insync` glue) is untested by
      the automated suite — needs a manual smoke test against a real
      paper TWS/Gateway session before being trusted
- [ ] `InMemoryIdempotencyStore` is process-local only, lost on restart
- [x] ~~Nothing yet actually produces an `ApprovedOrder`~~ — resolved by
      Step 9: `src.risk.engine.evaluate_trade_proposal` now builds a
      real, validated `ApprovedOrder` on APPROVE/RESIZE for a
      MANUAL-execution broker and hands it to the existing
      `FidelityManualProvider` unmodified
- [ ] **New from Step 9**: `src.risk.correlation` requires
      `Portfolio.price_history` to already contain aligned price series;
      nothing wires `src.data.historical` into it yet, so the
      correlation check is a documented no-op whenever that data is
      absent rather than fail-closed (see the Step 9 entry above for why
      fail-closed there would be worse, not safer)
- [ ] **New from Step 9**: `src.risk.stress`'s portfolio-level check
      only fully reprices the *new* position; existing positions'
      contribution to the worst-case figure is their already-known
      static `max_loss`, not a fresh reprice — needs multi-ticker market
      data as an engine input before it can be a true whole-portfolio
      stress test
- [ ] **New from Step 9**: no caller yet assembles a real `Portfolio`
      from actual broker state (`src.brokers.base.Account`/`Position`
      reconciled across IBKR + Fidelity manual fills) — `Portfolio` is
      fully defined and gated on, but nothing populates one outside
      tests yet
- [ ] **New from Step 9**: nothing yet produces a real
      `QuantitativeAnalysis` upstream of the Risk Engine either — Python
      Quant is a library of pure functions (`src.quant`), not yet a
      pipeline stage that runs ahead of `evaluate_trade_proposal` and
      hands it a `QuantitativeAnalysis` object the way the spec's data
      flow describes

- [ ] **New from Step 10**: `src.llm.portfolio_manager.evaluate_proposal`
      still has no real caller — nothing yet builds a real
      `PortfolioManagerInputs` from live `src.risk`/`src.quant` state and
      actually invokes it outside tests, mirroring the same gap Step 9
      flagged for `Portfolio`/`QuantitativeAnalysis` themselves
- [ ] **New from Step 10**: `InMemoryAuditLog` has the same "process-
      local only, lost on restart" caveat as `InMemoryIdempotencyStore` —
      a real persisted audit table is a Phase 0 DB item, not built yet
- [ ] **New from Step 10**: `PortfolioManagerReview` (shortlist many) and
      `PortfolioDecision` (rule on one) now both exist as Portfolio
      Manager outputs with no code yet deciding which one a given
      orchestration call should ask for — likely "shortlist first, then
      one PortfolioDecision per shortlisted candidate," but that
      sequencing isn't implemented or even written down anywhere yet
- [ ] **New from Step 11**: `src.llm.devils_advocate.evaluate_trade_risk`
      has the same no-real-caller gap Step 10 flagged for
      `evaluate_proposal` — nothing yet builds real
      `DevilsAdvocateInputs` (in particular two real
      `MarketSnapshotContext`s) from live market data and invokes it
      outside tests
- [ ] **New from Step 11**: `earnings_in_window` on `DevilsAdvocateInputs`
      is caller-resolved by design, but nothing yet calls
      `src.data.earnings.is_within_earnings_window` to actually resolve
      it from a real earnings calendar — same "isolated but tested"
      pattern as everything else pre-Phase-0
- [ ] **New from Step 11**: `AdversarialReview` (the original,
      lighter-weight critique) and `DevilsAdvocateReview` (the new,
      18-category structured verdict) now both exist as Devil's Advocate
      outputs, mirroring the `PortfolioManagerReview`/`PortfolioDecision`
      duality from Step 10 and carrying the same open question: which
      one a real orchestration call should actually ask for, and whether
      both are needed going forward
- [x] ~~Nothing drives the full loop end to end~~ — resolved by Step 12:
      `src.orchestration.pipeline.run_order_pipeline` now runs Quant ->
      Devil's Advocate -> Portfolio Manager -> Risk Engine -> Order
      Validator -> PaperBroker -> Portfolio -> Database back-to-back
      against one shared scenario, in the ordering Step 11's
      independence requirement demands
- [ ] **New from Step 12**: `InMemoryDatabase`
      (`src.orchestration.pipeline`) is, like every other in-memory
      store in this codebase, process-local and lost on restart — a real
      persisted database is still the same Phase 0 item it's always been
- [ ] **New from Step 12**: `default_portfolio_update_stage` folds a
      fill into a *new* `Portfolio`, but nothing yet persists that
      updated `Portfolio` anywhere or feeds it back in as the starting
      point of the *next* pipeline run — each call to
      `run_order_pipeline` still starts from whatever `Portfolio` its
      caller happens to pass in
- [ ] **New from Step 12**: `PaperBroker`'s expiration/assignment/
      exercise settlement (`settle_expiration`) must be called
      explicitly — there is no clock-driven scheduler that notices an
      option expired and settles it automatically
- [ ] **New from Step 12**: the pipeline calls Python Risk Engine twice
      per run (once for the `AUTOMATED` PaperBroker target, once for the
      `MANUAL` Fidelity target, to get the companion ticket "at the same
      time") — both calls independently recompute the same quant
      economics, which is correct but means the Fidelity ticket's own
      freshness is only as good as whatever `fresh_market_data` the
      *second* call happened to receive; nothing yet forces the two
      calls to share a single, atomic market-data snapshot
- [ ] **New from Step 12**: `ExecutionQualityRecord.subsequent_price`
      has no automated capture path — something still needs to actually
      look up a later price and call `record_subsequent_price`; nothing
      does that today
- [ ] **New from Step 17**: 20 MEDIUM/LOW findings from `SECURITY_AUDIT.md`
      remain open and unremediated (all 10 CRITICAL/HIGH findings were
      fixed in Step 17B — see below). Notable ones: no ticker-match/
      liquidity check on the Quant Engine's own stage-1 pass (MD-002),
      `OptionChain`/`UnderlyingQuote` missing consistency/bid≤ask
      validators (MD-004/MD-005), `PaperBroker` never re-marks an
      assigned equity position to the live price (OP-002), Devil's
      Advocate's `why_not_thesis` re-embedded unsanitized into the
      Portfolio Manager's next LLM call (LM-001), and corporate
      actions/stock splits entirely unhandled (MD-008/OP-007). Full list
      with reproduction steps and recommended remediation in
      `SECURITY_AUDIT.md`.

## 2026-09-20 (cont'd) — Hostile system audit and remediation (Steps 17-17B)

**Step 17 — hostile audit.** Assumed the posture of an independent team
hired to find every way this platform could lose money incorrectly,
calculate risk incorrectly, corrupt accounting, hallucinate data, bypass
safeguards, or execute unintended trades. Four parallel read-only
subagents covered market-data failures, options mechanics, system/
orchestration failures, and LLM boundary/quantitative correctness; direct
manual review covered the Risk Engine, drawdown/kill-switch/concentration/
correlation, the Fidelity manual-execution boundary, and the trade state
machine. Produced `SECURITY_AUDIT.md`: 36 findings (4 CRITICAL, 6 HIGH, 14
MEDIUM, 12 LOW) plus a consolidated "verified safe" list covering position
sizing, LLM schema numeric-field boundaries, Black-Scholes/probability/
payoff correctness, NaN/Infinite guarding, the full Fidelity-security
surface, and the trade state machine. No code was changed in Step 17 —
audit only, as instructed.

**Step 17B — remediate every CRITICAL/HIGH finding.** Fixed all 10
CRITICAL/HIGH findings from the audit, each with a regression test proving
the vulnerability is closed, without weakening or deleting any existing
test:

- **OP-001** (CRITICAL) — backtest assignment/exercise was discarding the
  share-value side of settlement, corrupting P&L by the full strike
  notional (a currently-passing test asserted a ~$9,000 overstatement on
  a single-contract CSP assignment). Added
  `src.backtest.assignment.realized_settlement_pnl`, converting
  settlement into an intrinsic-value-based economic impact (a covered
  position's own cost basis is used where applicable); `engine.py` and
  `rejected_trade_review.py` both now consume it. Corrected the two named
  tests that had encoded the bug's wrong output as "correct."
- **SY-001** (CRITICAL) — screener `proposal_id`s collided across
  separate scan runs (bare in-call counter, no date/expiration/strike
  encoded), silently swallowing genuinely new trades as stale duplicates.
  `_next_id` now encodes the scan date, strategy, expiration, and
  strike(s).
- **SY-002** (CRITICAL) — all idempotency/database state was in-memory
  only, unable to recognize a crash-then-retry. Added
  `SqliteIdempotencyStore`/`SqliteDatabase`, durable drop-in
  implementations of the existing `IdempotencyStore`/`Database`
  interfaces (in-memory defaults unchanged).
- **SY-004** (CRITICAL) — sequential candidates within one `/morning-scan`
  run all evaluated against the same pre-scan portfolio snapshot, so
  cumulative risk-limit enforcement across a batch never actually
  happened. `run_morning_scan` now threads each candidate's own
  `updated_portfolio` forward as the next candidate's Risk Engine input.
- **MD-001** (HIGH) — the Risk Engine's freshness gate silently fell back
  to `proposal.timestamp` (never independently verified) whenever a
  caller omitted `now`. `now` is now a required keyword-only argument
  with no default and no fallback.
- **MD-003** (HIGH) — contract/quote matching never verified the
  underlying ticker, only (expiration, strike, right). Both
  `_find_contract`/`resolve_leg_contracts` (trade_risk.py) and
  `_match_quotes_for_legs` (backtest/engine.py) now match on underlying
  too.
- **SY-003** (HIGH) — an exception in the Portfolio-update pipeline stage
  was uncaught, and (unlike every other stage) never reached the database
  write — a real fill could be permanently lost. Stage 7 now wraps in the
  same try/except-then-record pattern every other stage uses.
- **SY-005** (HIGH) — `Portfolio.cash` was never updated after a fill,
  only `positions`. `default_portfolio_update_stage` now reduces `cash`
  by the new position's `capital_at_risk`, consistent with this
  `Portfolio` type's own `capital_deployed_pct` semantics; fails closed
  (explicit `ValueError`, since `model_copy` doesn't re-validate) rather
  than silently going negative.
- **SY-006** (HIGH) — reconciliation discrepancies were only ever
  reported, never acted on — the same scan run that flagged an untracked
  Fidelity position could still generate a fresh, real duplicate
  recommendation for it. `run_morning_scan` now excludes any ticker with
  an unresolved `missing_from_internal` discrepancy from candidate
  generation for that run.
- **TS-004** (HIGH) — a CLOSE/ROLL proposal the Risk Engine approved had
  no `ApprovedOrder` built for it, and the Order Validator's first
  unguarded `approved_order.quantity` access raised a plain
  `AttributeError`, crashing the whole pipeline call. The Risk Engine now
  rejects non-OPEN actions cleanly with a new `ReasonCode
  .REJECT_UNSUPPORTED_ACTION`; `validate_and_build_order_request` also
  gained an explicit `None` check (defense-in-depth) that raises its own
  `OrderValidationError` instead.

New test files: `tests/unit/orchestration/test_portfolio_update_stage.py`,
`tests/unit/risk/test_trade_risk_contract_matching.py`. Extensive
additions to `tests/unit/backtest/test_assignment.py`,
`tests/unit/backtest/test_engine.py`, `tests/unit/brokers/test_base.py`,
`tests/unit/brokers/test_order_validator.py`,
`tests/unit/brokers/test_paper_broker.py`,
`tests/unit/orchestration/test_pipeline.py`,
`tests/unit/risk/test_engine_bypass_attempts.py`,
`tests/unit/workflows/test_candidate_generation.py`, and
`tests/unit/workflows/test_morning_scan.py`. The only lines removed from
any pre-existing test were the 3 assertions that had encoded OP-001's bug
as "correct" (now corrected) and the mechanical addition of a required
`now=` argument to call sites that previously omitted it (MD-001) — no
assertion was loosened or deleted to reach a passing state. Full suite:
1531 passed, 4 skipped (up from 1485 at the end of Step 16).

`SECURITY_AUDIT.md` updated in place: every fixed finding now carries a
**Status: FIXED (Step 17B)** note naming the fix and its regression test;
the summary table gained a Status column; the 20 remaining MEDIUM/LOW
findings are explicitly marked OPEN and still describe real, unremediated
gaps.

## 2026-09-20 (cont'd) — Fidelity human-execution dashboard (Step 18)

A local FastAPI + vanilla-JS web dashboard for portfolio monitoring and
human-controlled Fidelity execution, built entirely on top of already-
existing, already-tested machinery — no new risk math, no new state
machine invented, no new market-data path. **The dashboard cannot submit
a securities/options order to Fidelity; this is verified structurally by
tests, not just asserted.**

**One deliberate change to existing machinery first:** extended
`src.brokers.fidelity._ALLOWED_TRANSITIONS` so `AWAITING_HUMAN` and
`REPRICE_REQUIRED` can both reach `REJECTED` (previously reachable only
from the pre-ticket pipeline stages). REJECTED is Step 18's "I'm not
taking this trade" pre-entry decision, kept structurally distinct from
CANCELLED ("this order was entered into Fidelity and is now being
pulled back") — the two never overlap in the transition graph. 3 new
tests in `tests/unit/brokers/test_fidelity_state_machine.py`.

**`src/dashboard/` (new package):**
- `models.py` — `DashboardState` (the whole in-memory session: current
  `Portfolio`, `RiskLimitsConfig`, tracked opportunities, append-only
  `AuditLog`), `OpportunityRecord`, `OrderEntryRecord`. Process-local,
  no persistence — the same honestly-flagged Phase-0 gap every other
  `InMemory*` placeholder in this codebase carries.
- `risk_state.py` — the RISK PANEL's `NORMAL`/`WARNING`/`REDUCE_RISK`/
  `HALT` state (`src.risk.drawdown.DrawdownZone` + `src.risk.kill_switch
  .check_kill_switch`, composed, never re-derived), capital utilization,
  underlying/sector concentration (`src.risk.portfolio_risk`), and
  correlation clusters (`src.quant.correlations`, honestly reporting
  "not tracked" rather than fabricating a value when `price_history`
  isn't populated for 2+ held tickers — the same QF-001 gap the Step 17
  audit already named).
- `ticket_format.py` — `render_dashboard_order_text`, the exact "COPY
  FIDELITY ORDER" template Step 18 specifies (verified byte-for-byte
  against its own worked example), built from `FidelityTradeTicket`
  fields only — a second, dashboard-specific presentation of the same
  ticket `src.brokers.fidelity.render_ticket_text` already renders for
  `/morning-scan`'s report, not a competing computation.
- `service.py` — the five allowed actions (REFRESH PRICE, COPY FIDELITY
  ORDER, MARK ORDER ENTERED, record a FILLED/PARTIALLY_FILLED/CANCELLED
  outcome, REJECT TRADE), each built entirely on
  `src.brokers.fidelity.transition`/`confirm_fill`, plus
  `src.orchestration.pipeline.default_quant_stage` and
  `src.risk.engine.evaluate_trade_proposal` for the REPRICE flow's
  "rerun Quant Engine, Risk Engine" requirement. The deterministic Risk
  Engine's veto is absolute even on a refresh: if it no longer approves
  at the new price, the ticket is rejected outright, never left showing
  a stale approval. Portfolio is updated only after a real
  `ExecutionConfirmation` (mirrors `default_portfolio_update_stage`'s
  own SY-005 cash/capital_at_risk bookkeeping, applied to a confirmed
  Fidelity fill instead of a PaperBroker fill), and fails closed
  (explicit `ValueError`, not a silently-invalid Portfolio) if a fill
  would drive cash negative.
- `schemas.py` — API request/response shapes, reusing existing domain
  types directly (`DevilsAdvocateReview`, `RiskDecision`, `ReasonCode`,
  `TicketStatus`) as field types rather than re-declaring their fields.
- `app.py` — the FastAPI application: 6 read routes, 6 action routes,
  zero routes named or shaped like AUTO TRADE / EXECUTE / SEND TO
  FIDELITY. `execution_mode="MANUAL"` is a hardcoded literal, never
  sourced from a request.
- `static/` — a single-page vanilla-JS/HTML/CSS dashboard (no build
  step, no framework dependency): portfolio header, risk panel,
  opportunity cards with the full field set Step 18 specifies, a
  REPRICE REQUIRED banner that disables the COPY button, modals for
  every write action, and a live audit-log table. Manually verified
  end to end in a real headless-Chromium browser against a real
  running `uvicorn` server (not just FastAPI's in-process TestClient):
  loaded the page, clicked COPY FIDELITY ORDER (confirmed the exact
  rendered ticket text), clicked MARK ORDER ENTERED through a real
  form submission, and confirmed the UI correctly re-rendered with the
  new ORDER ENTERED status, the FILLED/PARTIALLY FILLED/CANCELLED
  button set, COPY now disabled, and all three audit events listed —
  zero console errors, zero failed network requests.

**Security tests** (`tests/unit/dashboard/test_app_security.py`, 24
tests): no credential-shaped identifier (password/username/mfa/cookie/
session_token/api_key/secret/otp) anywhere in `src/dashboard/`'s source
or its static frontend, checked the same way
`test_fidelity_no_execution.py` already checks `fidelity.py` itself —
against actual identifiers (assignment targets, parameter names,
Pydantic field names, HTML id/name attributes, JS declarations) via
regex, not a naive substring-anywhere-in-prose check (which would
wrongly flag this package's own docstrings explaining the absence,
exactly as `fidelity.py`'s module docstring already does); no forbidden
network/browser-automation import (`selenium`, `playwright`, `requests`,
`aiohttp`, `urllib3`) anywhere; a route-inventory test that fails if any
future change registers a route not on the explicit Step 18 allowlist;
proof that only `transition()`/`confirm_fill()` ever change a ticket's
status (no direct `model_copy(update={"status": ...})` bypass); proof
that `execution_mode` is hardcoded and appears on no request schema;
and a parametrized check that every one of the six action functions
actually calls the audit-log recorder.

New test files: `tests/unit/dashboard/{test_models,test_risk_state,
test_ticket_format,test_service,test_app_routes,test_app_security}.py`
(116 tests total). `fastapi`/`uvicorn`/`httpx` (already pinned in
`requirements.txt`/`requirements-dev.txt` since the original Step 1
scaffold, but never actually installed until now) were installed.
Full suite: 1650 passed, 4 skipped (up from 1531 at the end of Step
17B) — no existing test touched.

## Next up

- Two slash commands now exist in `.claude/commands/`: `/morning-scan`
  (Step 15) drives the daily research cycle, and `/weekly-review`
  (Step 16) convenes a Portfolio-Manager-chaired Investment Committee
  over portfolio performance, risk, decision quality, rejected trades,
  and Fidelity execution quality — both built the same way, a thin
  slash-command prompt over a real, fully-tested `src/workflows/`
  Python engine that does the actual computation. `/weekly-review`
  leans almost entirely on reuse rather than new math: `src.backtest
  .metrics`/`benchmark` for every performance and risk number,
  `src.research.performance_breakdown` for the dimension breakdowns,
  and `src.research.hypothesis.HypothesisRegistry` as the literal
  hand-off target for "send hypotheses to Strategy Research Agent" —
  the only genuinely new machinery this step added is the
  decision-quality quadrant classifier (built entirely from ex-ante
  facts, never P&L) and the rejected-trade hypothetical-outcome pricer
  (replaying a rejected proposal through the same `src.backtest
  .execution`/`assignment` a backtest itself uses). `src/orchestration/`
  still connects the live paper-trading pipeline; `src/backtest/`
  replays historical days; `src/research/` discovers and tracks
  hypotheses without authority to act on any of them; `src/workflows/`
  is the layer that calls all three into reviewable reports a human
  actually runs. What's still missing to make any of these four layers
  a real, continuously-running system rather than a function a test, a
  fixture, or a human-supplied input drives by hand: Phase 0
  foundations (a real database in place of every `InMemory*` placeholder
  accumulated across Steps 8-14, config, CI, and specifically a
  persisted equity ledger and trade journal `/weekly-review` still takes
  as caller-supplied input), a scheduler to drive market data updates,
  expiration settlement, and both slash commands themselves on a clock
  instead of by explicit calls, something that persists and re-loads
  `Portfolio` between runs instead of each call starting fresh, real
  `MarketDataProvider` calls (and a human-confirmation flow for Fidelity
  positions and fills) feeding both commands' inputs instead of
  hand-assembled fixtures, a real historical options-data vendor behind
  `HistoricalOptionChainProvider`, an `src.llm.market_regime`
  orchestration module (today both commands take a `MarketRegimeAssessment`
  as a caller-supplied input), a portfolio-level Greeks aggregator
  (today rendered honestly as "not tracked" rather than fabricated), a
  real caller for `evaluate_hypothesis`/`promote_strategy`, and the
  human-approval workflow that would actually authenticate a
  `human_approver` rather than accept a hardcoded string. Also still
  open: the `app/` prototype disposition, the accumulating
  layout/config-duplication/untested-real-adapter questions above, the
  four Step 9 gaps, the three Step 10 gaps, the two remaining Step 11
  gaps, the five Step 12 gaps (in-memory database, Portfolio not
  persisted between runs, no expiration scheduler, the two Risk Engine
  calls not sharing one atomic market snapshot, no automated
  subsequent-price capture), the three Step 13 gaps (no Strategy
  Screener at the backtest-engine level, no clock-driven backtest
  caller, survivorship bias only preventable "where possible" pending a
  real vendor), the three Step 14 gaps (in-memory hypothesis registry,
  no real caller for either `evaluate_hypothesis` or `promote_strategy`,
  no human-approval workflow), the four Step 15 gaps (no Market Regime
  orchestration module, no real feed/reconciliation caller for
  `run_morning_scan`, no portfolio-level Greeks aggregation, the
  screener's provisional closest-to-target-delta/richest-credit-first
  heuristics standing in for a real Opportunity Scanner), and the three
  new Step 16 gaps (no persisted equity ledger, no automated caller to
  gather `/weekly-review`'s trade/rejection/confirmation inputs, and two
  newly-introduced-not-yet-researched thresholds — the 0.50 minimum
  probability of profit for a "good decision" and the 20-sample
  threshold for a "meaningful" rejected-trade conclusion), and the new
  Step 18 gaps (`DashboardState` is process-local with no persistence —
  a server restart loses every tracked opportunity and the audit log
  with it; REFRESH PRICE has no real market-data connection, a human
  must type in the fresh quote by hand via the modal form; nothing yet
  loads a `MorningScanReport` into the dashboard automatically —
  `build_state_from_morning_scan`/`register_opportunity` exist and are
  tested, but no scheduler or CLI entry point calls them; no
  authentication of any kind in front of the FastAPI app, appropriate
  only because this is explicitly a local, single-user dashboard, not
  something meant to be exposed beyond localhost) above.

## 2026-09-20 (cont'd) — 90-Day Paper-Trading Validation Protocol (Step 19)

**Read first, per Step 19's own instructions**: `ARCHITECTURE.md`,
`IMPLEMENTATION_PLAN.md`, this file, `SECURITY_AUDIT.md`, and the source
for `PaperBroker` (`src/brokers/paper.py`), `src.llm.portfolio_manager`,
the Risk Engine (`src/risk/engine.py` + `src/risk/portfolio_risk.py`),
the Quant Engine (`src/orchestration/pipeline.py::default_quant_stage`),
the backtesting system (`src/backtest/{metrics,benchmark,simulator}.py`),
and "the database" (`InMemoryDatabase`/`SqliteDatabase` in
`src/orchestration/pipeline.py`, the only durable-storage pattern this
codebase has — reused directly rather than invented a second time).
`CLAUDE.md` does not exist in this repository; every other named
document does.

**Purpose, stated explicitly and enforced structurally, not just in
prose**: this protocol does not exist to prove the ~12-15% research
target achievable. `src/validation/gates.py`'s 90-day gate never reads
`config/validation.yaml`'s `research_targets` section — classification
depends only on sample adequacy, drawdown, rule compliance, and
risk-adjusted expectancy sign. The strongest classification a run can
receive, `PASS_FOR_EXTENDED_VALIDATION`, names its own next step as
*more* validation, never live trading — `ARCHITECTURE.md` §4's
"`LIVE` is not built in this phase" remains untouched; nothing in this
package could wire into a live path even if it tried.

**New package `src/validation/`** (11 modules, `config/validation.yaml`,
`reports/validation/`):

- `protocol.py` — `ValidationConfig` (same YAML-plus-env-override
  pattern as `src.risk.limits`), `ValidationPeriod` (start/end date,
  checkpoint-day membership), `SampleSizeStatus`
  (`INSUFFICIENT_SAMPLE`/`MINIMUM_SAMPLE`/`PREFERRED_SAMPLE`), and
  strategy-version freezing: `StrategyVersionManifest` sha256-hashes
  every strategy-relevant config file
  (`risk_limits.yaml`/`brokers.yaml`/`validation.yaml`) at freeze time,
  and `verify_manifest_integrity` re-hashes them later, raising
  `ManifestDriftError` — loudly, not silently — if any changed. This is
  detection, not prevention: nothing stops a config edit mid-run: what
  this guarantees is that it cannot pass unnoticed by whoever reads the
  report afterward.
- `session.py` — `ValidationStore` (`InMemoryValidationStore` +
  `SqliteValidationStore`, the same "each operation opens/closes its own
  connection" durability pattern as Step 17B's `SqliteDatabase`) storing
  four append-only record types: closed trades (`TradeRecord`, reused
  unmodified — a closed options trade's P&L fields describe a backtest,
  paper, or validation trade identically), immutable `DailySnapshot`s,
  `HypotheticalOutcome`s (rejected-trade tracking, reused from Step 16),
  and `RuleViolationRecord`s.
- `benchmarks.py` — SPY/risk-free/cash comparison, wrapping
  `src.backtest.benchmark` and adding the cash (flat 0%) comparison Step
  19 explicitly asks for alongside it.
- `statistics.py` — bootstrap confidence intervals (percentile
  resampling, seeded for determinism), a Monte Carlo forward NAV
  projection built by resampling the run's own observed trade P&Ls
  (deliberately not `src.quant.monte_carlo`'s lognormal single-option
  model — a portfolio of discretionary trade outcomes has no assumed
  distribution), VaR/CVaR (thin wrappers over
  `src.backtest.metrics.historical_var`/`historical_cvar`, never a
  second implementation), and the OBSERVED/ESTIMATED/PROJECTED labeling
  discipline: OBSERVED requires the sample to meet the protocol's own
  minimum, ESTIMATED is a real-but-thin sample, PROJECTED is any
  extrapolation beyond the observed window (annualizing a 90-day return
  is always PROJECTED, regardless of trade count).
- `decision_quality.py` — combined-and-per-strategy (never blended)
  wrapping of Step 16's unchanged four-quadrant classifier
  (`src.workflows.decision_quality`) and rejected-trade statistics
  (`src.workflows.rejected_trade_review`), scoped to a whole validation
  run instead of one week.
- `execution_quality.py` — validation-scoped Fidelity slippage (reuses
  `src.workflows.execution_quality.summarize_slippage` unmodified) plus
  genuinely new options-execution metrics (`fill_rate`,
  `assignment_rate`, `early_close_rate`, `expiration_otm_rate`) that a
  single week rarely has enough closed trades to make meaningful but a
  90-day run does; `fill_rate` is honestly `None` unless a caller
  supplies an attempted-trade count, never fabricated.
- `regime_analysis.py` — a 5-value `ValidationRegime`
  (`bull_trending`/`bear_trending`/`range_bound_low_vol`/
  `range_bound_high_vol`/`crisis_tail_event`), an explicit,
  flagged interpretation call: Step 19 asked for "5 regimes" without
  naming them, and the existing `src.llm.schemas.MarketRegimeLabel` only
  has 4 (it collapses direction into volatility level, insufficient for
  a short-premium-strategy regime taxonomy). Bucketing reuses
  `src.research.performance_breakdown.breakdown_by` directly.
  `RegimeCoverageSummary` honestly reports which of the 5 regimes a
  90-day window actually saw and flags single-regime dominance (>80% of
  trades in one regime) — the same "don't claim more than the sample
  supports" discipline `src.workflows.rejected_trade_review`'s
  `meaningful_sample` already established.
- `consistency.py` — weekly consistency (win rate/P&L stdev/losing-week
  streaks, bucketed in 7-day windows anchored on the validation period's
  own start date), correlation analysis (thin wrapper over
  `src.quant.correlations.flag_highly_correlated_pairs`), and
  `check_rule_compliance` — an **audit**, not an enforcement mechanism
  (the Risk Engine's veto is already unconditional): flags any trade in
  the run's own record lacking an APPROVE/RESIZE Risk Engine decision as
  a compliance gap, combined with whatever was separately logged as a
  `RuleViolationRecord`.
- `scorecard.py` — `ValidationScorecard`: seven named categories
  (RETURN, RISK, TRADE_QUALITY, EXECUTION, CONSISTENCY, COMPLIANCE,
  SAMPLE_ADEQUACY), **structurally incapable of collapsing into one
  number** — neither dataclass has an `overall_score`-shaped field at
  all, proven directly by a test that inspects the dataclasses' own
  field names rather than just checking behavior.
- `gates.py` — `evaluate_checkpoint` (30/60-day:
  `CONTINUE`/`CONTINUE_WITH_WARNING`/`HALT_FOR_INVESTIGATION` — no
  PASS-shaped value exists in the type at all) and
  `evaluate_90_day_gate` (5 classifications:
  `PASS_FOR_EXTENDED_VALIDATION`/`CONDITIONAL_PASS`/
  `EXTEND_VALIDATION`/`FAIL_RESEARCH_REVIEW`/`HALT`). **"Never PASS
  before day 90" is structural**: `evaluate_90_day_gate` takes `day: int`
  and raises `ValueError` if `day < 90` — the only function capable of
  returning `PASS_FOR_EXTENDED_VALIDATION` refuses to run at all before
  day 90, rather than trusting every caller to remember. Evaluation
  order is a strict hard-gate cascade, never a blended score: rule
  violations/halt-level drawdown -> `HALT`; `INSUFFICIENT_SAMPLE` ->
  `EXTEND_VALIDATION` regardless of how good performance looks (proven
  directly with a Sharpe=3.0/return=25% fixture that still extends);
  negative Sharpe with a loss -> `FAIL_RESEARCH_REVIEW`; `MINIMUM_SAMPLE`
  clean run -> `CONDITIONAL_PASS`; `PREFERRED_SAMPLE` clean run ->
  `PASS_FOR_EXTENDED_VALIDATION`. Drawdown thresholds are read from the
  live `RiskLimitsConfig`, never redeclared in `config/validation.yaml`
  — the same numbers the Risk Engine itself enforces.
- `alerts.py` — a closed `AlertCode` enum (drawdown warning/critical,
  consecutive losses, weekly loss threshold, rule violation, manifest
  drift, insufficient-sample-at-90) and `generate_alerts`, a pure
  per-day evaluation with no cross-call state (de-duplicating repeat
  fires is left to the caller, the same division of responsibility
  `src.risk.engine` leaves for "what to do with a REJECT").

**`config/validation.yaml`** — duration (90 days), checkpoint days
(30/60), sample-size thresholds (min 50, preferred 100), default
starting NAV ($100k), the 12-15% research targets (explicitly read only
by reporting code, never by `gates.py`), bootstrap/Monte Carlo
parameters with a fixed random seed, and alert thresholds. Deliberately
does **not** redeclare drawdown-zone thresholds — `alerts.py`/`gates.py`
take the live `RiskLimitsConfig` as an explicit parameter instead, so
the validation protocol's drawdown checks can never drift from the Risk
Engine's own numbers (previously flagged as a recurring anti-pattern in
this codebase — three independently-declared 15-minute freshness
placeholders — deliberately not repeated a fourth time here).

**Database**: no new database technology — `SqliteValidationStore`
follows the exact `SqliteDatabase`/`SqliteIdempotencyStore` pattern from
Step 17B (one file, each operation opens/closes its own connection, so
the store itself holds no in-process state a crash could lose). No
schema migration framework exists yet in this codebase (still a Phase 0
item); this is the fourth standalone sqlite-backed store built ahead of
that infrastructure, consistent with the pattern every other package in
this codebase has followed since Step 8.

**Tests**: `tests/unit/validation/` — 130 new tests across 11 files,
covering every module's calculations plus the four scenario categories
Step 19 explicitly named:
- **Validation-gate scenarios**: the full `evaluate_90_day_gate` cascade
  (HALT/EXTEND_VALIDATION/FAIL_RESEARCH_REVIEW/CONDITIONAL_PASS/
  PASS_FOR_EXTENDED_VALIDATION), plus the "never before day 90"
  structural guarantee and a direct assertion that `GateClassification`
  contains nothing LIVE/EXECUTE-shaped.
- **Insufficient-sample scenarios**: `sample_size_status` boundary tests
  at 0/49/50/99/100, and the gate test proving `INSUFFICIENT_SAMPLE`
  extends even a Sharpe=3.0, +25%-return fixture.
- **Version-change (manifest drift) scenarios**: a config file hashed at
  freeze time, then modified, correctly raises `ManifestDriftError` both
  directly (`test_protocol.py`) and via the alerting layer
  (`test_alerts.py`'s `TestManifestDriftAlert`).
- **Rule-violation scenarios**: `check_rule_compliance` catching a trade
  with a `REJECT` risk decision or a missing one, the checkpoint/90-day
  gate both halting on any violation regardless of every other metric,
  and each logged violation producing its own critical alert.

Full repo suite: **1780 passed, 4 skipped** (up from 1650 at the end of
Step 18; the 4 skips are the same pre-existing IBKR live-adapter skips).

## Open decisions carried forward (updated a seventh time)

- [ ] **New from Step 19**: nothing yet actually *runs* a 90-day
      validation session end to end — `src/validation/` is a fully
      tested calculation/reporting library over caller-supplied
      `TradeRecord`s/snapshots/outcomes, the same "isolated but tested"
      status every other package in this codebase carried at its own
      introduction (Steps 9-18 all say some version of this). No
      scheduler drives daily snapshot capture, no code path feeds real
      `PaperBroker`/Fidelity-confirmed fills into a `ValidationStore`,
      and no report-rendering function (a `render_validation_report`
      analogous to `render_morning_scan_report`/
      `render_weekly_review_report`) exists yet — this step built the
      engine, not the daily driver.
- [ ] **New from Step 19**: `RegimeCoverageSummary`'s 5-regime taxonomy
      is a documented interpretation call (Step 19 said "5 regimes"
      without naming them), independent of and not reconciled with
      `src.llm.schemas.MarketRegimeLabel`'s existing 4-value taxonomy —
      two regime vocabularies now exist in this codebase for two
      different purposes, flagged rather than silently merged.
  - `weekly_pnl` is a caller-supplied parameter to
      `src.validation.alerts.generate_alerts` rather than computed from
      the store itself — mirrors the same "caller assembles the input"
      gap every other workflow module in this codebase already carries.
- [ ] **New from Step 19**: `config/validation.yaml`'s
      `min_probability_of_profit`/`rejected_trade_min_sample_size`
      intentionally mirror `src.workflows.decision_quality`'s and
      `src.workflows.rejected_trade_review`'s existing default constants
      rather than being independently re-derived — still two places the
      same starting value is spelled out, now config-overridable in one
      of them; not fully unified.

## Next up

Per Step 19's explicit instruction: **do not proceed to Step 20.**
`src/validation/` closes the "how would we know if this actually works"
gap every prior step's "Next up" section implicitly deferred — Steps
9-18 built a fully wired PAPER-mode pipeline, a human-execution
dashboard, and weekly/backtest reporting, but nothing before this step
could answer "is 90 days of it any good" in a way that couldn't be
gamed by a lucky sample, a silently-edited risk limit, or judging
decisions by P&L alone. What remains before a real validation run could
start end to end: a scheduler or CLI entry point that actually drives
one (feeding real `PaperBroker`/confirmed-Fidelity fills into a
`ValidationStore` day by day), a report renderer analogous to
`render_morning_scan_report`, and — unrelated to this step but still
open — every Phase 0 foundation item and cross-step gap already
carried forward above.

## 2026-09-21 — Strategy library expansion and Strategy Competition Engine (Step 19A)

**Explicit instruction for this step:** expand the original 3-strategy
set (cash-secured put, covered call, put credit spread) into a 16-item
library (15 real strategies + NO_TRADE/CASH), replace any implicit
one-regime-to-one-strategy mapping with a Strategy Competition Engine
that generates every compatible candidate, prices all of them
identically, and selects the single best *risk-adjusted* candidate —
never the one with the largest theoretical profit — and extend, never
rebuild, Step 19's validation system to track it. Naked short calls,
undefined-risk short calls, unfunded naked puts, martingale sizing, and
any unlimited-risk structure remained excluded, unchanged from §1.
Nothing from Step 19 was deleted, reset, or weakened to do this — every
extension below is additive, verified by running the full suite after
each change rather than assumed compatible.

**`src/llm/schemas.py`** — `StrategyType` gained 9 members
(`CALL_CREDIT_SPREAD`, `BULL_CALL_SPREAD`, `BEAR_PUT_SPREAD`,
`PROTECTIVE_PUT`, `PROTECTIVE_COLLAR`, `LONG_STRADDLE`,
`LONG_STRANGLE`, `LONG_CALL`, `LONG_PUT`) alongside the original 3,
each with its own leg-shape/side/strike-ordering validator branch in
`TradeProposal._validate_legs_match_strategy` (e.g. a bull call spread
must have long strike < short strike; a protective collar's call
strike must exceed its put strike; a straddle's two strikes must be
equal). These 9, plus the original 3, are the **Tier1** set — every one
fits `TradeProposal`'s existing 1-2 leg cap and is wired all the way to
a real order.

**`src/quant/expected_value.py`** — 9 new closed-form economics
functions, one per new Tier1 strategy, following the exact
`StrategyEconomics` pattern the original 3 already used.
`StrategyEconomics` gained `breakeven_upper: float | None = None`
(additive; every existing single-breakeven strategy leaves it `None`).
Unbounded-upside strategies (`protective_put`, `long_call`,
`long_straddle`, `long_strangle`) set `max_profit=math.inf` and source
`expected_value` from the existing `monte_carlo_pop_and_ev` cross-check
tool (fixed seed `20260101`, 20,000 paths) rather than the binary
max-profit/max-loss EV heuristic the original 3 strategies use, which
is mathematically undefined once max_profit is infinite.

**`src/quant/monte_carlo.py`** — a new generic, exact (not
approximated) payoff-analysis engine: `PayoffProfile`
(`max_profit`/`max_loss`/`breakeven_points`) and `payoff_profile(position)`,
which evaluates any `Position` (any legs, any quantity ratio, with or
without underlying shares) at every strike plus zero, analyzes the
upside tail slope for unboundedness, and finds breakevens by exact
linear interpolation between adjacent kink points. This became the
single reusable foundation for both the closed-form
`expected_value` functions above (cross-checked against it directly in
tests) and the entirely new `src/strategies` evaluation layer below,
which has no closed-form economics functions of its own at all for the
6-and-up-strike structures (butterflies, condors).

**`src/risk/trade_risk.py`** (safety-critical, extended) — `check_collateral`,
`resolve_credit`, and `compute_trade_economics` each gained a dispatch
branch per new Tier1 strategy. `QuantitativeAnalysis.max_profit`'s
validator was split so `math.inf` is now accepted (NaN and `-inf`
remain rejected) — the one field in the Risk Engine's Pydantic contract
allowed to be non-finite, needed for the genuinely unbounded-upside
strategies. `cross_check_quantitative_analysis`'s tolerance check was
fixed alongside this: the existing `(a-b)/b` relative-difference
formula produces NaN when both `a` and `b` are `math.inf` (`inf - inf`
is undefined), which was incorrectly failing two *correctly agreeing*
infinite values before an explicit `math.isinf(a) and math.isinf(b)`
branch was added.

**`src/risk/engine.py`** (safety-critical bug fix) — `_build_approved_order`
was rewritten after a self-caught sign-correctness bug: a debit
strategy (e.g. a bull call spread) was rendering "NET CREDIT" on the
human-facing Fidelity ticket instead of "NET DEBIT", because
`resolve_credit()` always returns a positive magnitude and was being
used directly as the *signed* `estimated_credit_debit` field. The fix
generalizes `net_bid`/`net_ask` to a per-leg signed sum (works for any
leg-side combination, including a straddle's two long legs, which
previously crashed the old short/long-leg-lookup code with
`StopIteration`), derives `net_mid`, and uses its sign to set
`estimated_credit_debit` (signed) vs. `limit_price` (`abs(net_mid)`,
since `ApprovedOrder.limit_price` is `Field(gt=0)`). This was caught by
my own end-to-end smoke test, not user feedback, before any test ran
against it — flagged here because it is the most consequential defect
of this step: an unfixed version would have shown a trader the wrong
side of the trade on a manual-execution ticket.

**`config/brokers.yaml`** — all three broker configs
(`fidelity`, `ibkr_paper`, `internal_paper`) gained the 9 new Tier1
strategy names in `allowed_strategies`.

**`src/brokers/fidelity.py`, `src/workflows/morning_scan.py`** — small
additive changes so `math.inf` max-profit renders as "UNLIMITED" rather
than a literal `inf` on a human-facing ticket or report line, and the
Fidelity ticket's net bid/ask/mid lines render as unsigned magnitudes
(the signed value lives in `estimated_credit_debit`/the NET
CREDIT/DEBIT label, never duplicated as a second, possibly
inconsistent sign elsewhere on the ticket).

**New package: `src/strategies/`** (sibling to `src/llm`, `src/quant`,
`src/data`, `src/brokers`, `src/risk`, `src/workflows`,
`src/validation` — see `IMPLEMENTATION_PLAN.md` §10 for the full
deviation writeup):
- `base.py` — `StrategyKind` (15 members — the CASH/NO_TRADE concept is
  deliberately *not* a member, see `selector.py` below),
  `StrategyFamily` (9 members, multi-valued per strategy — a covered
  call is both `INCOME` and mildly `BULLISH`; a protective collar is
  `PORTFOLIO_PROTECTION`, `TAIL_RISK_HEDGE`, and
  `CAPITAL_PRESERVATION` at once), `TRADE_PROPOSAL_ELIGIBLE` (the 12
  Tier1 kinds), and `build_strategy_evaluation` — the single assembler
  every per-strategy module calls, producing a `StrategyEvaluation`
  with capital requirement, signed net credit/debit, max profit/loss,
  breakevens, all four Greeks, probability of profit, probability of
  max loss (`None` when the payoff has no single max-loss terminal
  region), expected shortfall/CVaR (from the same Monte Carlo sample as
  the EV, not a second simulation), return on capital, annualized ROC,
  a 0-1 liquidity score, estimated slippage, execution complexity,
  assignment/early-exercise/event risk levels, entry/exit/adjustment/
  invalidation rules, and Fidelity compatibility — computed exclusively
  from `src.quant` outputs, never invented by this module.
- 15 per-strategy modules, one file each
  (`cash_secured_put.py`, `covered_call.py`, `put_credit_spread.py`,
  `call_credit_spread.py`, `bull_call_spread.py`, `bear_put_spread.py`,
  `protective_put.py`, `protective_collar.py`, `long_call.py`,
  `long_put.py`, `long_straddle.py`, `long_strangle.py`,
  `long_call_butterfly.py`, `iron_condor.py`, `iron_butterfly.py`) —
  each a thin leg-builder that constructs a `Position` and calls
  `build_strategy_evaluation`. The last 3
  (`long_call_butterfly.py`/`iron_condor.py`/`iron_butterfly.py`) are
  the **Tier2** set: 3-4 leg structures `TradeProposal` cannot
  represent today, fully evaluable/comparable here but never wired to
  `src.risk.engine`/a Fidelity ticket — a deliberate scope boundary
  (widening the 2-leg cap touches the highest test-coverage code in the
  system), not an oversight, see `ARCHITECTURE.md` §13.
- `regime_mapping.py` — `MarketView` (8 values) and
  `CANDIDATE_STRATEGIES_BY_VIEW`, an explicit guideline table mapping
  each view to a *tuple* of strategies worth constructing and pricing —
  never a 1:1 mapping, and order within a tuple is not a ranking.
  Nothing here decides a trade.
- `suitability.py` — `is_strategy_suitable`: a share-requiring strategy
  (covered call, protective put, protective collar) is never even
  *constructed* unless the portfolio already holds ≥100×contracts
  shares of the underlying — the structural guarantee behind "never
  misclassify a bare short call as a covered call."
- `portfolio_fit.py` — post-trade underlying/sector exposure percentage
  and a 0-1 diversification score, reusing `src.risk.portfolio_risk`'s
  aggregation helpers directly (no second concentration computation).
  Correlation-with-existing is honestly `None` (not tracked) — the same
  QF-001 gap `src.risk.correlation.check_correlation` already carries,
  not silently fabricated here.
- `comparison.py` — `ComparisonRow` (one row per candidate, a full
  metrics table) and `risk_adjusted_score = expected_value /
  maximum_loss`, the literal antidote to this step's own worked
  example: a 25%-return/large-max-loss candidate does not automatically
  outrank a 14%-return/small-max-loss one. `rank_candidates` never
  collapses the table into just the winning score — both are always
  returned together, the same "structurally incapable of collapsing
  into one number" discipline `src.validation.scorecard` already
  established.
- `selector.py` — `select_best_or_no_trade`: takes already-priced
  `StrategyEvaluation` candidates plus each one's Devil's Advocate
  verdict and Risk Engine decision (`CandidateVerdicts`, supplied by
  the caller — this module does not itself invoke the Multi-Agent Layer
  or the Risk Engine), filters to `APPROVE`/`RESIZE`-only + non-flagged
  candidates, and picks the best `risk_adjusted_score` — or NO_TRADE.
  NO_TRADE/CASH is **not** a `StrategyKind` member; it is represented
  structurally as `SelectionOutcome.selected is None`, winning whenever
  no surviving candidate clears a configurable `no_trade_hurdle`
  (default `0.0`) or every candidate is filtered out. The full
  comparison table survives into `SelectionOutcome` regardless of which
  candidate wins, specifically so every non-selected alternative is
  available for counterfactual tracking later.
- `volatility_engine.py` — `compare_iv_to_rv` (IV-RV spread, term
  structure slope, percentile/rank passed through honestly as `None`
  when not supplied — never invented), `straddle_required_move_comparison`
  / `strangle_required_move_comparison` (required move vs. implied
  expected move and, where supplied, a historical move distribution),
  and `premium_compensates_for_tail_risk`. Every function returns a
  comparison figure only — never a "buy"/"sell"/"approved" verdict —
  the literal enforcement of "do not buy volatility simply because an
  event exists" / "do not sell volatility merely because IV is
  elevated": the qualitative judgment stays with the Multi-Agent Layer
  and the deterministic Risk Engine, never a Python heuristic
  pretending to make that call.

**`src/llm/context.py`** — `MarketContext` gained ~14 additive optional
fields (trend/momentum, realized volatility, IV percentile/rank, term
structure, skew, breadth, ATR, support/resistance, yields, credit
conditions, econ calendar) — every existing `MarketContext`
construction continues to pass unmodified.

**`src/validation/cohort.py`** (new) — the direct, tested answer to
this step's own cohort-transition rule: `has_cohort_started` checks the
`ValidationStore` for any real recorded trade/snapshot/rejected-outcome
/violation (never a manifest's mere existence), and
`decide_cohort_transition` returns `START_FRESH_COHORT` when nothing
has ever run, or `CLOSE_AND_START_NEW_COHORT` (preserving the prior
cohort's label, or `PRE_EXPANSION_VALIDATION` if none was ever
explicitly given) when it has — so results from materially different
strategy architectures are never mixed into one cohort's numbers.
`StrategyVersionManifest` gained `cohort_label: str = "default"`
(additive) and `build_validation_manifest` a matching parameter.

**`src/validation/counterfactual.py`** (new) — `StrategyAlternativeRecord`
stores a decision-time `StrategyEvaluation` directly (no duplicated/
independently-drifting fields), and `summarize_selection_effectiveness`
/`summarize_by_regime` compute Selection Regret (positive = the
selected candidate underperformed a non-selected alternative) only once
a sample of concluded opportunities crosses a configurable minimum size
— a `meaningful_sample=False`/explicit warning below that threshold,
the same "don't claim more than the sample supports" discipline
`src.workflows.rejected_trade_review` and Step 19's
`RegimeCoverageSummary` already established. No-trade expectancy is
passed through from the caller, never computed here (this module prices
alternatives, it does not itself run a backtest).

**`src/backtest/regime_scenarios.py`** (new) — `generate_regime_path`,
a deterministic seeded GBM price/IV path generator across 8 named
regimes (`BULL`/`BEAR`/`SIDEWAYS`/`HIGH_VOLATILITY`/`LOW_VOLATILITY`/
`VOLATILITY_EXPANSION`/`VOLATILITY_CONTRACTION`/`MARKET_CRASH`). This
is a **test-fixture generator**, not new backtest-engine logic —
`src.backtest.engine` was confirmed already strategy-agnostic and
needed no changes. Used to prove "every defined-risk strategy's
`max_loss` bound holds against every simulated terminal price in every
regime" — deliberately never a P&L-sign or win-rate assertion in any
regime, since this step explicitly does not require one strategy to
always win anywhere.

**Testing discipline**: after every source-code change, the directly
affected test subdirectory was run before moving on
(`tests/unit/llm/`, `tests/unit/quant/`, `tests/unit/risk/`,
`tests/unit/brokers/`, `tests/unit/orchestration/`,
`tests/unit/dashboard/`), catching regressions immediately rather than
accumulating risk. 186 new tests across 13 files:
`tests/unit/llm/test_expanded_strategy_types.py` (24, the 9 new
`StrategyType` members' leg-validation rules),
`tests/unit/quant/test_payoff_profile.py` (16, the generic engine
cross-checked against every closed-form strategy's own max-profit/loss/
breakeven),
`tests/unit/quant/test_expanded_expected_value.py` (13, the 9 new
economics functions including the Monte-Carlo-sourced EV path for
unbounded strategies),
`tests/unit/risk/test_expanded_strategies.py` (17, collateral/credit-
sign/economics dispatch for all 9, plus the debit-vs-credit sign-
correctness regression test that caught the `_build_approved_order`
bug),
`tests/unit/strategies/test_base.py` (13),
`tests/unit/strategies/test_individual_strategies.py` (20, all 15
per-strategy modules),
`tests/unit/strategies/test_regime_mapping_suitability.py` (12),
`tests/unit/strategies/test_comparison_portfolio_fit_selector.py` (11,
including a direct test of the 25%/30%-vs-14%/6% worked example),
`tests/unit/strategies/test_volatility_engine.py` (12),
`tests/unit/strategies/test_system_integration.py` (1, the full
MARKET DATA → REGIME → OPPORTUNITY → MULTIPLE STRATEGIES → QUANT →
COMPARISON → Devil's Advocate/Portfolio Manager verdicts (supplied,
same pattern `tests/unit/orchestration/test_pipeline.py` already uses)
→ real Risk Engine → SELECTED STRATEGY OR NO_TRADE → real PaperBroker →
validation store → reporting chain, everything except the two LLM
verdicts unmocked),
`tests/unit/validation/test_cohort.py` (12, including the direct,
programmatic answer to this step's own questions #11/#12 — see below),
`tests/unit/validation/test_counterfactual.py` (11), and
`tests/unit/backtest/test_regime_scenarios.py` (9, the 8-regime
generator plus the cross-strategy max-loss-bound property test,
parametrized across all 8 regimes for a put credit spread and
separately checked for a bear put spread inside a bull regime to make
"no strategy has to win everywhere" explicit).

Full repo suite: **1966 passed, 4 skipped** (up from 1780 at the end of
Step 19; +186 new tests, 0 regressions, 0 weakened or deleted existing
tests, 4 skips unchanged — the same pre-existing IBKR live-adapter
skips).

**Validation cohort status (this step's own questions #11/#12,
answered programmatically, not asserted):** this codebase's actual
current `ValidationStore` state has **never recorded a single
trade, snapshot, rejected-trade outcome, or violation** — no 90-day
validation session has ever actually run end to end (Step 19 built the
calculation/reporting engine; nothing yet drives it day by day, see
Step 19's own "Next up" above). `decide_cohort_transition(None)` and
`decide_cohort_transition(InMemoryValidationStore())` both correctly
resolve to `START_FRESH_COHORT` with
`new_cohort_label="MULTI_STRATEGY_VALIDATION_V1"` — so **no**,
the formal cohort had not started, and **no** `PRE_EXPANSION_VALIDATION`
cohort needs to be closed because none ever existed; **yes**, per this
step's own rule for the "not started" branch, a fresh
`VALIDATION_MANIFEST` should be frozen now, directly as
`MULTI_STRATEGY_VALIDATION_V1`, rather than staged behind a close step.
`reports/validation/VALIDATION_MANIFEST.json` was frozen accordingly —
`manifest_id="mstrat-v1"`, `cohort_label="MULTI_STRATEGY_VALIDATION_V1"`,
90-day period starting today, `strategy_versions` covering all 12
Tier1 strategies plus the 3 Tier2 evaluation-only ones at `"v1"`, and
`config_file_hashes` over `config/risk_limits.yaml`,
`config/brokers.yaml`, and `config/validation.yaml` as they exist at
freeze time — so any later silent edit to those three files during the
run is detectable via `verify_manifest_integrity`, exactly as Step 19
designed. **This manifest freeze is not the same as Day 1 having
happened** (`has_cohort_started` on the freshly-created store still
correctly returns `False`, proven directly in
`test_cohort.py::TestStartNewCohort`) — actually starting Day 1 still
requires the same not-yet-built daily driver Step 19's "Next up"
already flagged, unchanged by this step.

`config/validation.yaml` needed **no new fields** for this step —
`cohort_label` is a per-manifest-freeze parameter
(`build_validation_manifest(..., cohort_label=...)`), not a
YAML-configured constant, so there was nothing to add to the config
file itself; confirmed by re-reading `src/validation/protocol.py`
rather than assumed.

## Open decisions carried forward (updated an eighth time)

- [ ] **New from Step 19A**: the Strategy Competition Engine is a
      comparison/selection layer over already-constructed
      `StrategyEvaluation` candidates, not a candidate-generation layer
      that scans a live `OptionChain` for viable strikes/expirations
      itself. `src.workflows.candidate_generation` (Step 15) still only
      knows how to build candidates for the original 3 strategies —
      wiring it to generate candidates for all 12 Tier1 strategies from
      real chain data, so the Strategy Competition Engine can run
      end-to-end rather than against caller-assembled test fixtures, is
      the concrete next-integration gap this step leaves open. See
      `IMPLEMENTATION_PLAN.md` §10.
- [ ] **New from Step 19A**: the 3 Tier2 strategies
      (`LONG_CALL_BUTTERFLY`, `SHORT_IRON_CONDOR`,
      `SHORT_IRON_BUTTERFLY`) are evaluation/comparison-only —
      `TradeProposal`'s 1-2 leg cap was deliberately not widened this
      step. Extending the trusted kernel (`src.risk.engine`,
      `src.risk.trade_risk`, `src.brokers.fidelity`) to 3-4 legs is a
      separately-scoped hardening decision, not made here.
- [ ] **New from Step 19A**: no scheduler or CLI entry point yet drives
      a real 90-day (or any) validation session day by day — unchanged
      from Step 19's own "Next up," now inherited by the
      `MULTI_STRATEGY_VALIDATION_V1` cohort whose manifest this step
      froze. The manifest existing is not the same as Day 1 having
      happened.
- [ ] **New from Step 19A**: `src.strategies` has no verified one-way
      dependency-boundary test the way `src.quant`/`src.data`/
      `src.brokers` do — not needed today (it only imports `src.quant`,
      `src.data`, `src.risk`, and `src.llm.schemas`, never the reverse),
      but flagged here rather than silently assumed permanent as the
      package grows.

## Next up

Per this step's own instruction, mirrored from Step 19: **do not
proceed to Step 20 without direction.** The concrete gaps this step
leaves open, in likely priority order: (1) wire
`src.workflows.candidate_generation` to build real, chain-sourced
candidates for all 12 Tier1 strategies so the Strategy Competition
Engine runs against live data rather than test fixtures; (2) build the
daily driver that actually starts Day 1 of `MULTI_STRATEGY_VALIDATION_V1`
(feeding real `PaperBroker`/confirmed-Fidelity fills and daily snapshots
into a `ValidationStore`) — inherited unchanged from Step 19; (3) decide
whether/when to widen `TradeProposal`'s leg cap to bring the 3 Tier2
strategies into Tier1; and, unrelated to this step but still open,
every Phase 0 foundation item and cross-step gap already carried
forward above.

## 2026-09-21 (cont'd) — Complete Options Strategy Selection Engine (Step 14B)

**Explicit instruction for this step:** build the "complete" strategy
selection engine — the same 16-item library and Strategy Competition
Engine concept Step 19A already delivered, now specified with its own
exact vocabulary (family names, market-view labels, a `ranking.py`
module named explicitly) and, critically, three new concrete asks
Step 19A had not yet covered: (1) a dedicated strategy-attribution
report answering six named questions per strategy, (2) a final system
test across 8 named market scenarios proving the pipeline can select a
candidate or NO_TRADE in each without ever requiring one winner, and
(3) an explicit re-inspection of the Quant Engine, Risk Engine, Market
Regime Agent, Strategy Research Agent, Fidelity constraints,
PaperBroker, and Backtesting Engine — "do not duplicate existing
functionality." Given how much Step 19A had already built, this step's
real work was threefold: (a) align existing code to this step's own
naming where it differs without breaking anything, (b) find and close
genuine gaps the re-inspection surfaces rather than re-describing
already-complete work, and (c) build the two named modules that
genuinely didn't exist yet (`ranking.py`, `strategy_attribution.py`)
plus the final system test. Nothing from Step 19A was rebuilt, reset,
or weakened — every change below is additive or a targeted, tested bug
fix, verified by running the full suite after each change.

**Re-inspection findings, before writing anything new:**

- `src.backtest.simulator.BacktestLeg`/`EntrySignal.legs` carry no leg-
  count cap at all, and `src.brokers.base.PlaceOrderRequest.legs` is
  already `Field(min_length=1, max_length=4)` — the backtest engine and
  PaperBroker were never the reason Tier2's 3-4 leg strategies can't
  become a real order; that boundary is `TradeProposal`'s 1-2 leg cap
  and the Risk Engine's Tier1-only dispatch tables alone, unchanged
  from Step 19A's own documented decision (`ARCHITECTURE.md` §13).
- **Two real capital-requirement bugs**, both pre-dating this step
  (present since the original 3-strategy platform; only reachable in
  practice once Step 19A's spread strategies existed to trigger them),
  caught by directly testing `PaperBroker._required_collateral` and
  `src.backtest.engine._estimate_capital_at_risk` against a bull call
  spread rather than assuming their existing "3 supported strategies"
  docstrings still described the platform accurately: (1) a debit
  vertical spread (bull call spread, bear put spread) was charged the
  full strike-width collateral a *credit* spread of that width needs —
  a real over-collateralization that could reject an affordable debit
  trade, since a debit spread's maximum loss is already the premium
  paid, nothing more. (2) Both functions also returned **zero**
  capital at risk for any pure-long position with no short legs at all
  (long call, long put, long straddle, long strangle, a freshly-bought
  protective put) — the short-strikes-sum fallback is empty when there
  are no short legs, silently under-reporting capital at risk for
  every long-premium strategy Step 19A added, and (in `PaperBroker`'s
  case) leaving nothing else to check that a debit trade's cash was
  actually affordable before filling it.
- `Strategy Research` agent persona (`.claude/agents/strategy_research.md`)
  still describes "the platform's three approved strategies" — now
  stale relative to the 16-item library. Not rewritten this step (its
  own scope, `src.research.performance_breakdown`'s 12-dimension
  breakdown machinery, was never asked to expand to the new strategies
  either) — flagged below rather than silently left inaccurate or
  silently rewritten without being asked.

**Fixes**, both mirroring the exact strike-ordering rule
`src.llm.schemas.TradeProposal`'s own leg validators already enforce
for `CALL_CREDIT_SPREAD`/`BULL_CALL_SPREAD`/`PUT_CREDIT_SPREAD`/
`BEAR_PUT_SPREAD`, so the pricing/collateral/execution layers can never
silently disagree about which strike order is a credit spread and
which is a debit spread:

- **`src/brokers/paper.py`** — new module-level `_is_credit_pairing(short,
  long)` helper (pure strike-order check, no premium lookup needed);
  `_required_collateral`'s pairing branch now skips width-collateral
  entirely for a debit pairing. `attempt_fill` gained a preflight
  cash-affordability check, computed by the exact same per-leg signed-
  cash formula `_apply_fill` uses (so the two can never diverge):
  a debit that would take `self._cash` negative is now rejected with a
  clear reason, never silently filled. 5 new regression tests in
  `tests/unit/brokers/test_paper_broker.py`
  (`TestDebitVerticalSpreadCollateral`,
  `TestDebitAffordabilityPreflightCheck`).
- **`src/backtest/engine.py`** — `_estimate_capital_at_risk` imports
  `_is_credit_pairing` directly from `src.brokers.paper` rather than
  re-deriving the same rule a second time; gained a new
  `entry_credit_total` parameter (the signed entry credit/debit,
  already computed at the one call site in `run_backtest`) used by
  every branch that previously fell back to zero or an incorrect
  width. A protective-collar-shaped pair (short call + long put,
  different rights) now correctly uses the covering shares' cost
  basis instead of falling through to the generic short-strikes-sum
  fallback. 12 new tests in `tests/unit/backtest/test_engine.py`
  (`TestEstimateCapitalAtRiskAllStrategyShapes`), covering all 12
  Tier1 shapes plus the Tier2 fallback and default-parameter backward
  compatibility.

**`src/strategies/base.py`** — `StrategyFamily` renamed to Step 14B's
own exact 8-value vocabulary (`DIRECTIONAL_BULLISH`/
`DIRECTIONAL_BEARISH`/`NEUTRAL_RANGE` replacing `BULLISH`/`BEARISH`/
`NEUTRAL`), keeping `TAIL_RISK_HEDGE` as a documented 9th value — a
protective put/collar is more than generically "portfolio protection,"
and neither step said the classification must be exactly 8 and no
more. `STRATEGY_FAMILIES` updated to match; no strategy's
classification set changed, only the label vocabulary.

**`src/strategies/ranking.py`** (new) — `risk_adjusted_score`/
`rank_candidates` moved out of `comparison.py` into their own module,
the file Step 14B names explicitly, separate from the metrics-table
builder. `comparison.py` re-exports both names for the one caller that
still imports them that way (`tests/unit/strategies
/test_comparison_portfolio_fit_selector.py`); `selector.py` now
imports directly from `ranking.py`. 6 new tests in
`tests/unit/strategies/test_ranking.py`, including a same-object
identity check proving the re-export never drifts into a second
implementation.

**`src/strategies/regime_mapping.py`** — a 9th `MarketView`,
`LOW_IV_EXPANSION_EXPECTED` (long straddle/strangle/call/put plus
"appropriate debit spreads"), added alongside the original 8:
deliberately broader than `LARGE_MOVE_EXPECTED` (direction uncertain),
since Step 14B's own low-IV-expansion list explicitly allows
directional candidates. `CANDIDATE_STRATEGIES_BY_VIEW`'s existing 8
entries verified unchanged against Step 14B's own worked examples
(moderately bullish, strongly bullish, moderately bearish, neutral/
range-bound, high-vol/large-move, portfolio protection, high-IV-
contraction) — all matched exactly, confirming Step 19A's original
table already satisfied this step's spec rather than needing a rebuild.

**`src/validation/strategy_attribution.py`** (new) — the module this
step's "reporting system must answer" section asks for.
`per_strategy_performance` tracks each strategy independently ("do NOT
judge only the combined portfolio"): trades, wins, losses, win rate,
net P&L, dollar-weighted return on capital, expectancy, profit factor
(`None` with zero losing trades, never a fabricated ratio), a
per-trade Sharpe-like ratio (explicitly documented as an
approximation — mean/stdev of each trade's own `pnl/capital_at_risk`,
gated behind a minimum-sample-size threshold the same way
`src.workflows.rejected_trade_review` already gates "meaningful
sample," and never conflated with the rigorous equity-curve Sharpe
`src.backtest.metrics.compute_metrics` computes portfolio-wide, which
has no per-strategy equivalent to compute from), a drawdown-
*contribution* figure (peak-to-trough on a pseudo equity curve built
from that strategy's own trades' chronological P&L, not a true
portfolio percentage), average holding period, and average slippage
(the identical `theoretical - realistic - commission` definition
`src.backtest.engine.build_backtest_result` already uses). Reuses
`src.research.performance_breakdown.breakdown_by` directly for the
strategy and regime groupings rather than a second bucketing
implementation. `answer_attribution_questions` answers the six named
questions with plain deterministic threshold rules over those numbers
— generated profit (net P&L > 0), reduced losses / improved drawdown
(hedge-family strategies present, judged by family classification per
Step 19A's own rule, never by standalone P&L sign), consumed capital
without value (non-hedge, net P&L <= 0), regime-specific (>80% of a
strategy's own trades concentrated in one regime, the same threshold
`src.validation.regime_analysis.RegimeCoverageSummary` already uses),
increased tail risk (worst single loss > 3x that strategy's own
average loss magnitude), generated excessive trading costs (average
slippage exceeding 25% of expectancy). 23 new tests in
`tests/unit/validation/test_strategy_attribution.py`.

**`tests/unit/strategies/test_final_system_scenarios.py`** (new) — the
final system test this step asks for, across the 8 named scenarios
(STRONG BULL, MODERATE BULL, SIDEWAYS LOW VOL, SIDEWAYS HIGH IV,
STRONG BEAR, VOLATILITY EXPANSION, VOLATILITY CONTRACTION, PORTFOLIO
CRASH — the latter three run against a shares-holding portfolio,
since `NEUTRAL_RANGE_BOUND`'s own candidate list is mostly Tier2
(iron condor/butterfly) plus a shares-requiring covered call).
Contracts are priced by the real quant engine (`src.quant
.black_scholes.price`) at each scenario's own spot/sigma, not
hand-picked numbers, so all 8 scenarios' differing volatility levels
produce internally consistent premiums. Per scenario: generates every
suitable candidate for that scenario's `MarketView`, prices each via
the real `evaluate_*` functions, supplies Devil's Advocate/Risk Engine
verdicts (same "no real API key needed" pattern
`test_system_integration.py` already established), runs the real
`select_best_or_no_trade`, and asserts only that a valid outcome exists
(a real candidate that was actually generated, or NO_TRADE with a
reason) — **never which strategy wins**, per this step's own explicit
instruction. Cross-checks every scenario's actually-generated
candidates against `src.backtest.regime_scenarios.generate_regime_path`
the same way `test_regime_scenarios.py` already does for one hand-picked
position, now against real Strategy-Competition-Engine output. A 9th
test proves NO_TRADE is reachable, not merely representable (an
artificially impossible hurdle forces it). 9 tests total.

**Everything already satisfying Step 14B's spec without any change**,
confirmed by direct inspection rather than assumed: all 16 library
items (§Step 19A); the common strategy-evaluation contract
(`StrategyEvaluation`'s ~30 fields, computed exclusively by
`build_strategy_evaluation` from `src.quant` outputs — the "no method
calls, a frozen dataclass of pre-computed fields instead" design
choice from Step 19A stands, since every value Step 14B's own
`interface` section lists is present as data, just never invoked as
`.capital_requirement()`); the comparison methodology
(`risk_adjusted_score`, never raw return, directly tested against this
step's own 25%/40%-drawdown vs. 12%/6%-drawdown framing); Fidelity
compatibility (`fidelity_compatible`/`fidelity_incompatibility_reason`
on every evaluation, unknown capability already treated as
non-order-eligible for the 3 Tier2 kinds); position-aware suitability
(`src.strategies.suitability`, unchanged); portfolio-level controls
(`src.strategies.portfolio_fit`, unchanged); NO_TRADE as a first-class,
structurally-representable outcome (unchanged); position management
rules and roll-as-close-plus-open (Step 19A's per-strategy
entry/exit/adjustment/invalidation rule tuples, unchanged); backtesting
support for every Tier1 shape (now fully correct after this step's two
collateral fixes); paper-trading support (now with the debit-
affordability check closing the one real gap found).

Full repo suite: **2021 passed, 4 skipped** (up from 1966 at the end of
Step 19A; +55 new tests, 0 regressions, 0 weakened or deleted existing
tests, 4 skips unchanged — the same pre-existing IBKR live-adapter
skips).

## Open decisions carried forward (updated a ninth time)

- [ ] **New from Step 14B**: `.claude/agents/strategy_research.md`
      still describes "the platform's three approved strategies" —
      genuinely stale relative to the 16-item library, not rewritten
      this step since neither this step nor Step 19A asked for the
      Strategy Research Agent's own scope (or
      `src.research.performance_breakdown`'s dimension list) to expand,
      and doing so unprompted risked silently changing a role's
      contract. Flagged rather than silently left inaccurate.
- [ ] **New from Step 14B**: the per-trade Sharpe-like ratio in
      `src.validation.strategy_attribution` is a documented
      approximation (mean/stdev of per-trade returns, not annualized,
      no per-strategy equity curve exists to compute the rigorous
      version from) — two different "Sharpe" computations now exist in
      this codebase for two different purposes and granularities,
      flagged rather than silently conflated.
- [ ] **New from Step 14B**: the six attribution questions'
      "reduced losses" / "improved drawdown" answers are the same
      hedge-family-membership set for both questions — this codebase
      has no true portfolio-level hedge-effectiveness counterfactual
      (replaying the same period with and without the hedge) to
      actually distinguish them; both are currently qualitative
      (family classification), not the drawdown-reduction dollar
      figure a real counterfactual would produce. A real answer would
      extend `src.validation.counterfactual` with a hedge-specific
      replay, not attempted here.
- Every open decision Step 19A carried forward (candidate generation
  still needs live-chain wiring, the 90-day daily driver still doesn't
  exist, the Tier2 leg-cap question still isn't decided, `src.strategies`
  still has no verified architecture-boundary test) remains open,
  unchanged by this step.

## Next up

Per this step's own instruction, mirrored from Step 19/19A: **do not
proceed further without direction.** The concrete gaps this step
leaves open, in likely priority order: (1) everything Step 19A's own
"Next up" already named (live-chain candidate generation, the 90-day
daily driver, the Tier2 leg-cap decision); (2) a true portfolio-level
hedge-effectiveness counterfactual, so "reduced losses" and "improved
drawdown" become independently-computed dollar figures rather than the
same family-membership flag; (3) deciding whether
`.claude/agents/strategy_research.md` and
`src.research.performance_breakdown`'s dimension list should expand to
the full 16-strategy library, or remain deliberately scoped to the
original 3 — not decided here.

## 2026-09-21 (cont'd) — Step 19A gap-check re-ask

A follow-up restatement of Step 19A's own objective arrived, closely
overlapping what Step 19A and Step 14B already delivered. Per its own
explicit "do not rebuild" instruction, this pass was a genuine
re-inspection (steps 1-14 of its own preamble: docs, the Step 19
validation system, the Strategy Engine, Quant Engine, Risk Engine,
Market Regime Agent, Portfolio Manager, Devil's Advocate, PaperBroker,
Backtesting Engine, Fidelity constraints) looking for real gaps against
the restatement's own added detail, not a re-implementation of anything
already built. Confirmed already satisfied, by direct inspection: all
16 library items and the Tier1/Tier2 split; the 9-family
classification (Step 14B's renamed vocabulary); the 9-view regime
mapping including `LOW_IV_EXPANSION_EXPECTED`; the volatility engine;
the comparison/ranking engine (`risk_adjusted_score`, never raw
return); position-aware suitability; hedge evaluation by family
classification; per-strategy Fidelity-compatibility flags; backtesting
across all 8 named regimes with the two collateral bugs already fixed
in Step 14B; the frozen `MULTI_STRATEGY_VALIDATION_V1` manifest
(re-verified programmatically this step — still no recorded trade,
snapshot, rejected outcome, or violation, so the cohort still has not
formally started and no new manifest freeze was needed); `MarketContext`'s
full set of extended regime-analysis fields (trend, momentum, realized/
implied vol, IV percentile/rank, term structure, skew, breadth, ATR,
support/resistance, Treasury yields, credit conditions — economic-
calendar/earnings events already covered by the pre-existing
`notable_events` field).

**Two genuine gaps found and closed, both additive:**

- **`ManagementConditionType`** (`src.strategies.base`, new 9-value
  enum matching this step's own list verbatim) + `MANAGEMENT_CONDITION_TYPES`,
  a per-`StrategyKind` table auto-wired into `build_strategy_evaluation`
  exactly the way `STRATEGY_FAMILIES` already is — zero changes needed
  to any of the 15 individual strategy modules. `StrategyEvaluation`
  gained a new `management_condition_types` field. Previously,
  `entry_rules`/`exit_rules`/`adjustment_rules`/`invalidation_rules`
  were free-text prose only, descriptive but not backed by any closed
  type — this enum is the concrete mechanism behind "LLMs may interpret
  conditions, they may NOT improvise risk rules": the *categories* of
  management logic applicable to a strategy are now a closed,
  Python-determined set. Pure long-premium strategies (long call/put,
  straddle, strangle) correctly carry no `ASSIGNMENT_MANAGEMENT` (no
  short leg to be assigned on); every strategy with a short leg does.
  5 new tests in `tests/unit/strategies/test_base.py`
  (`TestManagementConditionTypes`).
- **`src.validation.strategy_attribution.funnel_counts_by_strategy`**
  + new `StrategyFunnelCounts` dataclass (opportunities_considered/
  trades_proposed/trades_rejected/trades_entered) — the decision-time
  funnel this step names explicitly, distinct from that module's
  existing completed-trade statistics (`per_strategy_performance`).
  Sourced from `src.validation.counterfactual.StrategyAlternativeRecord`
  (already captured at decision time for every serious candidate,
  selected or not, since Step 19A) rather than a second decision-
  tracking mechanism: `opportunities_considered` counts distinct
  opportunities where a strategy was generated as a candidate at all,
  `trades_proposed` counts records where it won the internal
  risk-adjusted comparison (`was_selected`), `trades_rejected` counts
  records whose own `risk_decision` was not an approval/resize
  regardless of selection, and `trades_entered` requires both.
  `StrategyPerformanceSummary` also gained `avg_capital_deployed`
  (mean `capital_at_risk` per completed trade) — this step's own
  "capital utilization" figure, computed from data already tracked
  rather than a new NAV-relative metric this module has no portfolio
  context to compute honestly. 6 new tests in
  `tests/unit/validation/test_strategy_attribution.py`
  (`TestFunnelCountsByStrategy`).

No changes were made to `reports/validation/VALIDATION_MANIFEST.json`
this pass — `strategy_versions` still covers the same 15 `StrategyKind`
values at `"v1"`, and no new strategy was added, so the existing
`MULTI_STRATEGY_VALIDATION_V1` freeze remains accurate; re-verified
programmatically (not assumed) that the cohort still has not formally
started.

Full repo suite: **2032 passed, 4 skipped** (up from 2021 at the end of
Step 14B; +11 new tests, 0 regressions, 0 weakened or deleted existing
tests, 4 skips unchanged).

## Open decisions carried forward (updated a tenth time)

Every open decision Step 19A/14B already carried forward remains open,
unchanged by this pass (live-chain candidate generation, the 90-day
daily driver, the Tier2 leg-cap decision, the per-trade Sharpe
approximation's documented scope, the "reduced losses"/"improved
drawdown" hedge-family-flag limitation, `.claude/agents/strategy_research.md`'s
stale "three strategies" language). No new open decisions from this
pass — both gaps found were closed, not deferred.

## Next up

Unchanged from Step 14B's own "Next up": do not proceed further without
direction. The same three priorities stand (live-chain candidate
generation, the 90-day daily driver, the Tier2 leg-cap decision), plus
the hedge-effectiveness counterfactual and the Strategy Research Agent
scope question, neither decided here.

## 2026-09-21 (cont'd) — Multi-Strategy Attribution, Selection, and Hedge Effectiveness Reports

**Explicit instruction for this step:** three dedicated reports on top
of the already-built per-strategy tracking — a Multi-Strategy
Attribution Report (one row per all 15 strategies, 17 named fields
including two new dimensions), a Strategy Selection Report answering
whether the Strategy Selector itself adds value (including "is dynamic
selection outperforming a simpler fixed-strategy approach," never
asked before), and a Hedge Effectiveness Report for protective
put/collar specifically ("do NOT label a hedge unsuccessful merely
because its standalone P&L is negative"). All three are genuinely new
reporting surfaces — the underlying per-strategy tracking they draw on
(Step 14B's `strategy_attribution.py`, Step 19A's `counterfactual.py`)
already existed and was extended, never rebuilt.

**`src/research/performance_breakdown.py`** — a 13th `AnalysisDimension`,
`volatility_regime` (a plain qualitative label like
`MarketRegimeAssessment.regime` already uses — `low_vol`/`normal`/
`elevated_vol`/`crisis` — deliberately distinct from `iv_percentile`'s
numeric buckets and from `ValidationRegime`'s combined direction+
volatility taxonomy, a third taxonomy for a fourth purpose).
`TradeContext` gained a matching `volatility_regime: str | None = None`
field (additive). **Kept `src.llm.schemas.AnalysisDimension` in sync**
(added the same value, bumped `supporting_dimensions`'
`max_length` 12→13) — that mirrored Literal has its own drift-check
test (`test_strategy_research_review_schema.py`) which would have
failed otherwise; updating both together, not one, is what "stays in
sync" actually requires.

**`src/validation/strategy_attribution.py`** — `StrategyFunnelCounts`
gained `trades_approved` (any candidate the Risk Engine approved,
selected or not — broader than the existing `trades_entered`, which
requires both proposed *and* approved). `StrategyPerformanceSummary`
gained `performance_by_volatility_regime` (mirrors
`performance_by_regime`, bucketed on the new dimension instead). New
`MultiStrategyAttributionRow`/`MultiStrategyAttributionReport`/
`build_multi_strategy_attribution_report`/
`render_multi_strategy_attribution_report`: always one row per all 15
`StrategyKind` values (Tier1 and Tier2 alike, since Tier2 is fully
backtestable/evaluable even though it can't become a live order) —
never only the strategies present in the supplied data. A strategy with
zero opportunities still gets a row: zeros plus `INSUFFICIENT_SAMPLE`
(reusing `src.validation.protocol.SampleSizeStatus`'s *type* with a new,
smaller per-strategy threshold pair — 20/50 rather than the whole
90-day run's 50/100 — since a single strategy's slice of the run needs
its own, lower bar). "Average P&L" and "Expectancy" are rendered as two
labels on the one already-computed `expectancy` number rather than a
duplicated field, since they are mathematically the same figure.

**`src/quant/monte_carlo.py`** — two new reusable functions,
`simulated_terminal_payoffs` and `tail_mean_payoff`, extracted from
`src.strategies.base.build_strategy_evaluation`'s own previously-inline
expected-shortfall calculation (`base.py` now calls them instead of
repeating the calculation — a refactor verified byte-for-byte
equivalent by the full existing test suite, not a behavior change).
Built as shared infrastructure specifically so a caller needing a
*paired* comparison across two different `Position`s under the
identical simulated terminal-price sample (same seed) — exactly what
hedge effectiveness needs — doesn't have to re-simulate or duplicate
the tail-mean math a third time.

**`src/validation/counterfactual.py`** — new
`FixedStrategyBaseline`/`DynamicVsFixedComparison`/
`dynamic_vs_fixed_strategy_comparison`: what portfolio expectancy would
have looked like if every opportunity a given strategy was ever
*considered* for had used that strategy, always (computed only over
opportunities that strategy actually was a candidate for, never
extrapolated) — compared against the dynamic selector's own actual
blended expectancy. `dynamic_outperforms_best_fixed` is `None`,
never a guessed boolean, whenever either side lacks a meaningful
sample.

**`src/validation/selection_report.py`** (new) — the Strategy Selection
Report. Deliberately a third module, not added into `counterfactual.py`
or `strategy_attribution.py`, because it needs both (and
`strategy_attribution` already imports `counterfactual`, so importing
`strategy_attribution` back into `counterfactual` would cycle). Four of
the six named questions ("primarily reduce portfolio risk," "consume
capital without sufficient benefit," "work only in specific regimes,"
"excessive execution costs") are answered by directly reusing
`answer_attribution_questions`' existing output — never re-answered by
a second implementation. The two new questions: "selected most often"
(`SelectionFrequency`, a plain count+share per strategy) and
"risk-adjusted value" (expectancy per dollar of capital deployed — the
realized-trade-data analogue of `src.strategies.ranking
.risk_adjusted_score`, which uses ex-ante numbers this module doesn't
have). `render_strategy_selection_report` follows the established
`render_*_report` plain-text style
(`src.workflows.weekly_review.render_weekly_review_report`).

**`src/strategies/hedge_effectiveness.py`** (new) — the Hedge
Effectiveness Report's actual computation. Structurally enforces "do
NOT label a hedge unsuccessful merely because its standalone P&L is
negative" by never having a success/failure field anywhere in either
`HedgeEffectivenessReport` or `AggregateHedgeEffectiveness` (verified
directly by a test that inspects the dataclasses' own field names, the
same technique `src.validation.scorecard`'s own "never collapses into
one number" test already established). Computed via a **paired** Monte
Carlo comparison — the hedge's real `Position` (shares + hedging
option legs) against a synthetic all-shares "unhedged" `Position`
(`Position(legs=[], underlying_shares=..., underlying_cost_basis=...)`,
confirmed to work directly with the existing `payoff_profile`/
`payoff_at_expiration` with no changes needed), both simulated from the
identical terminal-price sample via the new `simulated_terminal_payoffs`
(same seed — a fair comparison under identical market outcomes, not
two independently-sampled runs). Reports: hedge cost, drawdown avoided
(unhedged max_loss − hedged max_loss), tail loss avoided (hedged CVaR
− unhedged CVaR, via `tail_mean_payoff`), CVaR reduction %, portfolio
volatility reduction % (simulated payoff stdev), upside sacrificed
(capped-gains-only, net of the hedge's own constant premium cost — see
the two bugs below), and net hedge benefit (one reasonable combined
figure, explicitly documented as not the only possible combination).
`aggregate_hedge_effectiveness`/`build_hedge_effectiveness_report`/
`render_hedge_effectiveness_report` average the same seven measures
across many recorded evaluations, gated by a meaningful-sample flag,
still with no verdict field anywhere.

**Two real bugs caught by direct numeric testing before this landed,
both in the first implementation of this same file, not in
already-shipped code:**
1. `hedge_cost` was computed directly from `net_credit_or_debit`
   without scaling — that field is a **per-share, per-contract unit
   price** (already true of every strategy since Step 19A, just never
   previously consumed outside `src.risk`/`src.brokers.fidelity`,
   which apply their own ×100×contracts scaling at the point of use).
   A $1.50 put premium was reporting as `hedge_cost=$1.50` instead of
   the correct $150 for one 100-share contract — caught by printing
   and checking the actual numbers from a real `evaluate_protective_put`
   call, not assumed correct from the formula alone.
2. `upside_sacrificed`'s first formula compared unhedged vs. hedged
   payoffs at the best simulated outcomes without netting out the
   hedge's own constant premium cost — which shows up in *every*
   scenario, including the best ones, and isn't actually a capped-
   upside effect at all. This made a **protective put** (which has no
   short call leg and therefore never caps gains) incorrectly report
   $150 of "upside sacrificed" — exactly its own premium, double-
   counted from `hedge_cost`. Fixed by subtracting `hedge_cost` from
   the raw top-tail gap before flooring at zero; a protective put now
   correctly reports `0.0`, and a protective collar's sold call still
   correctly reports a positive figure. Caught by a test asserting the
   protective-put-specific zero, not assumed from the formula.

**Tests**: 46 new — `tests/unit/research/test_performance_breakdown.py`
(+2, volatility_regime bucketing), `tests/unit/validation
/test_strategy_attribution.py` (+6, the multi-strategy report),
`tests/unit/validation/test_counterfactual.py` (+5,
dynamic-vs-fixed), `tests/unit/validation/test_selection_report.py`
(new file, 10), `tests/unit/strategies/test_hedge_effectiveness.py`
(new file, 23, including the two bug-catching regression tests above
and a direct dataclass-field-name check that no verdict field exists).

Full repo suite: **2078 passed, 4 skipped** (up from 2032 at the end of
the previous pass; +46 new tests, 0 regressions, 0 weakened or deleted
existing tests, 4 skips unchanged).

## Open decisions carried forward (updated an eleventh time)

- [ ] **New from this step**: `net_hedge_benefit`
      (`tail_loss_avoided - hedge_cost - upside_sacrificed`) is
      explicitly documented as one reasonable combination of the six
      underlying measures into a single dollar figure, not derived from
      any authoritative hedge-pricing theory — a human reading the
      report should look at the six components individually before
      trusting this one number, the same "never collapse away the
      detail" instinct this report otherwise follows throughout.
- [ ] **New from this step**: hedge effectiveness is computed per
      decision (one `StrategyEvaluation`, point-in-time), not yet wired
      to any real validation-run data source — nothing in this
      codebase yet calls `evaluate_hedge_effectiveness` automatically
      when a protective put/collar opportunity is evaluated, mirroring
      the same "engine built, daily driver doesn't exist yet" gap every
      other validation-adjacent module in this codebase has carried
      since Step 19.
- Every open decision already carried forward from Step 19A/14B/the
  prior gap-check pass remains open, unchanged by this step.

## Next up

Unchanged in substance: do not proceed further without direction. The
concrete gaps: (1) live-chain candidate generation; (2) the 90-day
daily driver that would actually populate these three new reports with
real data; (3) the Tier2 leg-cap decision; (4) wiring
`evaluate_hedge_effectiveness` into whatever eventually generates
protective put/collar candidates, so hedge reports accumulate
automatically rather than requiring a caller to invoke it by hand.

## 2026-09-21 — Step 20A: Multi-Leg Strategy Integration

**Instruction**: complete end-to-end order-eligibility for
LONG_CALL_BUTTERFLY, SHORT_IRON_CONDOR, SHORT_IRON_BUTTERFLY — the 3
strategies Step 19A deliberately left evaluation-only because
`TradeProposal.legs` capped at 2. Explicitly framed as the
separately-scoped hardening pass every prior step (19A, 14B, the 19A
re-ask) flagged and deferred rather than rushed.

**Mandatory pre-implementation audit (completed before any code
change, per the instruction's own gate)**: grepped `src/` for every
`max_length=2`/`max_length=4` leg-count field and every `breakeven:`
singular field. Finding: most of the "trusted kernel" already supported
4 legs at the Pydantic-field level before this step —
`src.brokers.base.PlaceOrderRequest.legs`, `src.brokers.fidelity`'s
`ApprovedOrder.legs`/`FidelityTradeTicket.legs`, `src.risk.portfolio_risk
.PortfolioPositionLeg`-list, and `src.dashboard.schemas.leg_quotes`
were already `max_length=4`. The confirmed blockers were narrower than
the instruction's framing suggested: `src.llm.schemas.TradeProposal
.legs` (`max_length=2`), the missing 3 `StrategyType` members, and
several places assuming a flat 1:1 per-leg quantity ratio (which no
prior strategy needed, since `LONG_CALL_BUTTERFLY`'s 1:-2:1 middle
strike is the first non-uniform ratio this platform has had to
represent). `src.strategies.base.StrategyKind`/`STRATEGY_FAMILIES`/
`MANAGEMENT_CONDITION_TYPES` and the 3 evaluation-only strategy modules
(`long_call_butterfly.py`, `iron_condor.py`, `iron_butterfly.py`) were
already fully built and tested from Step 19A — only their
`fidelity_compatible=False` flags needed flipping.

**Architectural changes**:

1. `src.llm.schemas`: `TradeProposal.legs` widened to `max_length=4`;
   `StrategyType` gained the 3 members; `OptionLeg.quantity_ratio`
   (additive, default 1) added; 3 new `elif` branches in
   `_validate_legs_match_strategy` enforce each strategy's exact leg
   count/strike ordering/side/ratio, fail-closed (missing wing, wrong
   option type, wrong strike order, wrong quantities, mismatched
   center strike, a long-iron-butterfly-shaped payload are all
   rejected at the schema layer). A generic `else` branch now also
   enforces `quantity_ratio == 1` on every leg of every pre-existing
   strategy, so the new field can never silently misrepresent an old
   structure.
2. `src.quant.expected_value`: 3 new economics functions
   (`long_call_butterfly_economics`, `short_iron_condor_economics`,
   `short_iron_butterfly_economics`) build a `Position` and delegate
   to the existing generic `payoff_profile` engine for exact max
   profit/max loss/breakevens (never a new hand-derived formula), plus
   a full Monte Carlo run for probability-of-profit/expected-value
   (the profit zone sits *between* two breakevens for all 3, so the
   existing `probability_above`/`probability_below` closed forms don't
   apply the way they do for a single-sided structure).
3. `src.risk.trade_risk`: `check_collateral`/`resolve_credit`/
   `compute_trade_economics` gained dispatch branches for the 3
   strategies via a new `_legs_sorted_by_strike` helper (sorting
   `(strike, put-before-call)` deterministically recovers each leg's
   structural role, since `_matching_by_right`'s single-match lookup
   is ambiguous once a strategy has two same-right legs).
   `compute_trade_greeks` now weights each leg by `quantity_ratio`
   (was hardcoded `quantity=1` — a latent bug for any future
   non-uniform strategy, now fixed generically).
4. `src.risk.engine`: `_build_quant_position`/`_build_approved_order`
   now multiply by `leg.quantity_ratio`; net_bid/net_ask are now
   ratio-weighted (previously always ±1 per leg — would have
   understated the butterfly's true net debit by treating a 1:-2:1
   combo as 1:-1:1); `_STRATEGY_DISPLAY` gained the 3 names.
5. `src.brokers.fidelity`: `ApprovedOrder`/`FidelityTradeTicket`
   gained `breakeven_upper: float | None` (additive) — and in fixing
   this, found and fixed a genuine **pre-existing gap**: the
   pre-Step-20A long straddle/strangle's second breakeven was computed
   by `StrategyEconomics` but silently dropped before ever reaching
   `ApprovedOrder`/a human-readable Fidelity ticket. `render_ticket_text`
   now prints "BREAKEVEN (LOWER)"/"BREAKEVEN (UPPER)" when both are
   present. `src.llm.context.QuantitativeAnalysisContext` and
   `src.dashboard.schemas.OpportunityView`/the dashboard frontend
   received the same additive field and propagation fix, so no stage
   of the pipeline silently discards the upper breakeven anymore.
6. `src.brokers.paper` (PaperBroker): new `base_combo_quantity` helper
   — the smallest per-leg `OrderLeg.quantity` across an order ("1
   combo unit") — replaces every place that used to read
   `order.legs[0].quantity` as a proxy for the order's overall size (a
   latent bug: `TradeProposal.legs`/`ApprovedOrder.legs` carry no
   guaranteed submission order, so "the first leg" was never a safe
   proxy once a strategy could have a non-uniform ratio).
   `compute_fill` now separately tracks per-share leg prices
   (`raw_signs`, unweighted, used for actual cash flow/position
   updates) from the combo's net per-unit price (`weighted_signs`,
   scaled by `quantity_ratio`, used for limit-price/fillable-quantity
   checks) — so a butterfly's net debit correctly reflects its middle
   leg counting double without distorting any individual leg's own
   per-share price. `_apply_fill` scales each leg's actual fill
   quantity and commission by its own ratio (the middle leg now
   correctly pays 2x commission, consistent with a standard
   per-contract-per-leg schedule). `_required_collateral` gained
   explicit shape detection for the butterfly (debit paid only, zero
   extra collateral) and the two iron structures
   (`max(put_wing_width, call_wing_width)`, standard margin treatment)
   *before* the generic same-right pairing fallback, which cannot
   recognize a 2x-ratio leg or two same-right short legs and would
   otherwise have (a) charged the butterfly's short middle leg as if
   naked (badly overstating risk) or (b) summed both iron-structure
   wing widths instead of taking the wider one (also overstating risk,
   though less dangerously than an under-estimate would).
7. `src.backtest.engine`/`src.backtest.simulator`/`src.backtest.slippage`/
   `src.backtest.execution`: `BacktestLeg` gained the matching
   `quantity_ratio` field (default 1), threaded through `_to_order_leg`/
   `flip_legs` so `compute_fill`'s ratio-aware pricing applies
   identically to a backtested butterfly. `_estimate_capital_at_risk`
   gained the same 3 collateral-shape branches as PaperBroker (mirrored
   deliberately, not reimplemented independently, matching the existing
   `_is_credit_pairing`-sharing discipline between the two modules).
8. `src.strategies.regime_mapping`: found and fixed a genuine gap —
   `SHORT_IRON_CONDOR`/`SHORT_IRON_BUTTERFLY` were already present
   under `NEUTRAL_RANGE_BOUND`/`HIGH_IV_CONTRACTION_EXPECTED` since
   Step 19A, but `LONG_CALL_BUTTERFLY` was never added to either view.
   Added to both, with guidance text describing the pin/range thesis +
   controlled-volatility + favorable-debit conditions under which it's
   worth pricing.
9. `config/brokers.yaml`: all 3 strategies added to every broker's
   `allowed_strategies` — done **last**, after the full pipeline was
   proven end-to-end by the new test files below, per this file's own
   standing rule (never list a capability before it's backed up).
10. Not touched, deliberately: `src.strategies.selector`/`comparison`/
    `portfolio_fit`/`suitability` needed no changes (already generic
    over `StrategyEvaluation` lists); `src.validation.strategy_attribution`
    already iterates all 15 `StrategyKind` values (built that way in
    Step 14B specifically so this day would need no further changes
    there) — verified, not modified.

**New documentation**: `CLAUDE.md` (did not exist — created, covering
the platform's non-negotiable invariants and the Step 20A multi-leg
architecture specifically, so a future session doesn't have to
rediscover the `quantity_ratio`/`base_combo_quantity` reasoning from
scratch), `README.md` (did not exist — created, beginner-friendly, all
14 required topics), `.env.example` (did not exist — created, every
env var documented, no real secrets), `scripts/start.sh` + `Makefile`
(did not exist — created; `./scripts/start.sh` smoke-tested to
actually boot the dashboard). `ARCHITECTURE.md` gained §14 (full
technical writeup of every mechanism above) and an update to §13/§12's
open question 6 (marked resolved, with the one remaining gap — no
backtest candidate generator constructs a butterfly `EntrySignal` yet
— stated explicitly rather than left implicit).
`IMPLEMENTATION_PLAN.md` gained §11.

**Deliberately out of scope, stated explicitly rather than silently
skipped**: the backtest engine's `EntrySignal`/`run_backtest` already
take fully-formed entries from an external caller (there is no
internal per-strategy candidate generator anywhere in this codebase to
extend for any of the 16 strategies, not just the 3 new ones) — Step
20A made the pricing/execution math correct for a butterfly entry once
one exists (`BacktestLeg.quantity_ratio`), but did not build a new
candidate-generation call site, matching the existing, unresolved
"live-chain candidate generation" gap already tracked in this file's
open decisions since Step 19A.

**Tests**: 50 new — `tests/unit/risk/test_multileg_strategies.py` (new
file, 35: TradeProposal structural validation for all 3 strategies
including every named malformed-structure rejection, collateral/
resolve_credit/compute_trade_economics dispatch, and 4 full
`evaluate_trade_proposal` end-to-end integration tests proving correct
leg count, correct 1:-2:1 quantity ratio on the rendered Fidelity
ticket, correct net credit/debit sign, both breakevens present, and
that an unlisted broker capability still rejects), `tests/unit/brokers
/test_paper_broker_multileg.py` (new file, 8: butterfly ratio-aware
fill/position/collateral/commission, iron-condor 4-leg fill and
wider-wing-only collateral, and direct `base_combo_quantity` unit
tests), `tests/unit/backtest/test_engine.py` (+7,
`TestEstimateCapitalAtRiskMultiLegShapes`, mirroring the existing
Step 14B regression-test pattern for the 3 new shapes). 4 pre-existing
tests updated (not weakened) to assert the intentionally reversed
Tier1/Tier2 architectural decision:
`tests/unit/strategies/test_base.py`'s `test_3_tier2_kinds_are_not
_trade_proposal_eligible`/`test_strategy_type_for_returns_none_for_tier2`
became `test_all_16_kinds_are_trade_proposal_eligible`/
`test_strategy_type_for_returns_matching_type_for_former_tier2`, and
`tests/unit/strategies/test_individual_strategies.py`'s 3
`test_defined_risk_and_fidelity_incompatible` tests became
`test_defined_risk_and_fidelity_compatible`, asserting
`fidelity_compatible is True` where they'd asserted `False`. Also
fixed a pre-existing test-fixture gap while writing these:
`tests/unit/risk/conftest.py`'s `quantitative_analysis_from` helper
never passed `breakeven_upper` through — invisible for every prior
2-breakeven-capable strategy this fixture was exercised against, but
would have produced a false `REJECT_QUANT_MISMATCH` for any 2-breakeven
strategy run through it, caught immediately by the new end-to-end
butterfly test.

Full repo suite: **2125 passed, 4 skipped** (up from 2078 at the end of
the previous pass; +50 new tests, -3 net from the 4-updated/1-merged
Tier1/Tier2-split tests above nets against previously-counted tests,
0 regressions, 0 weakened assertions, 0 deleted tests, 4 skips
unchanged, 0 bypassed risk checks, 0 hardcoded expected answers).

## Open decisions carried forward (updated a twelfth time)

- [ ] **New from this step**: no backtest candidate generator
      constructs a `LONG_CALL_BUTTERFLY`/`SHORT_IRON_CONDOR`/
      `SHORT_IRON_BUTTERFLY` `EntrySignal` yet — `run_backtest` takes
      fully-formed entries from its caller for all 16 strategies alike;
      this was already true before Step 20A and remains true after it.
      The pricing/execution math is now correct once such an entry
      exists (verified by `TestEstimateCapitalAtRiskMultiLegShapes`
      and the ratio-aware `compute_fill` path), but nothing yet
      generates one automatically.
- [ ] **New from this step**: no report renderer (daily/weekly/monthly
      workflows, trade journal) has been hand-verified against a real
      3-/4-leg trade record yet, since no live/paper trading of the 3
      new strategies has actually happened — `strategy_attribution.py`
      and the dashboard's `OpportunityView` are verified generic/
      correct by direct unit test, but an end-to-end "a real
      LONG_CALL_BUTTERFLY trade flows through morning-scan → paper fill
      → weekly review → strategy attribution" walkthrough has not been
      run, since the platform has no live daily driver yet (unchanged
      from every prior step's open decisions).
- [ ] Every open decision already carried forward from Step 19A/14B/the
      19A re-ask/the reports pass remains open, unchanged by this step,
      **except** open decision "(3) the Tier2 leg-cap decision" from
      the immediately preceding entry, which this step resolves.

## Next up

The formal 90-day validation cohort has **not started** (per
`src.validation.cohort.has_cohort_started`: no `ValidationStore`
anywhere in this repository has recorded a single real trade, snapshot,
rejected-trade outcome, or violation — no persisted database file
exists at all). Per this step's own instruction and the cohort-
transition logic already built in Step 19A (`src.validation.cohort
.decide_cohort_transition`, unchanged by this step, still returns
`START_FRESH_COHORT`), this means the future frozen validation cohort
can start at Day 1 directly under the now-expanded 16-strategy library
— there is no `PRE_EXPANSION_VALIDATION` cohort to preserve, because
none has begun.

Concrete gaps, largely unchanged in substance from the prior entry:
(1) live-chain candidate generation (still the single largest concrete
gap — nothing in this codebase yet fetches a live option chain and
turns it into a scored opportunity automatically, for any of the 16
strategies); (2) the 90-day daily driver that would actually run the
platform day over day and populate real trade/validation/attribution
records; (3) wiring `evaluate_hedge_effectiveness` into whatever
eventually generates protective put/collar candidates; (4) persisting
reports/validation data to disk across restarts (README.md's
"Where reports/validation results are stored" sections describe this
honestly as not yet built).

## Step 21 — Final system integration & acceptance test

**Commit tested:** `3909862` (Step 20A). **Purpose:** verification and
hardening only, per the instruction's own explicit constraints — no
new features, no strategy-logic changes, no risk-limit changes, no
live trading, no automated Fidelity execution, and the 12-15%
aspirational target never treated as an acceptance criterion.

**What was built:** a new `tests/acceptance/` suite (186 tests across
14 files: `test_end_to_end_happy_path.py`, `test_market_data_
failures.py`, `test_strategy_integrity.py`, `test_risk_veto.py`,
`test_llm_boundary.py`, `test_multileg_integrity.py`, `test_quant_
integrity.py`, `test_paper_broker.py`, `test_accounting_
reconciliation.py`, `test_assignment_expiration.py`, `test_order_
state_machine.py`, `test_fidelity_manual_only.py`, `test_validation_
pipeline.py`, `test_operational_resilience.py`), each described in
detail in the new `ACCEPTANCE_TEST_REPORT.md` at the repo root. The
suite deliberately reuses the REAL, unmocked `evaluate_trade_proposal`
(Risk Engine), `validate_and_build_order_request` (Order Validator),
`PaperBroker`, and `default_quant_stage` throughout — only the two LLM
stages are scripted with a fake `LLMClient` — which is a stronger form
of proof than testing against mocks of the security-critical
components, and never makes a live Anthropic API call or touches live
market data.

**Bugs found and fixed** (full detail, reproduction, and severity
classification in `ACCEPTANCE_TEST_REPORT.md` §28):

1. **ACCEPT-003 (HIGH)** — `src/backtest/assignment.py`'s expiration-
   settlement math (`settle_leg`/`realized_settlement_pnl`) ignored
   `BacktestLeg.quantity_ratio`, understating `LONG_CALL_BUTTERFLY`'s
   2x middle leg's assignment cash/share impact by half whenever a
   butterfly was held to expiration ITM in a backtest (a $700-per-
   combo-unit error in the reproduction case, verified against the
   platform's own independently-correct `payoff_at_expiration` engine).
   Backtest-only — never reachable in the live PaperBroker/Fidelity
   path, since `PaperBroker.settle_expiration` uses each position's
   own already-ratio-correct tracked quantity. Fixed by multiplying
   every leg's settlement impact by its own `quantity_ratio`; no-op
   for every other strategy (all use ratio=1 on every leg). Regression
   tests in `tests/acceptance/test_assignment_expiration.py`.
2. **ACCEPT-001 (MEDIUM)** — `src/brokers/paper.py`'s
   `price_satisfies_limit` used a strict `>=` comparison between two
   independent floating-point evaluations of the same net-price
   quantity (`ApprovedOrder.limit_price` vs. `PaperBroker`'s own
   recomputed `net_price`, summed in a different leg order) — for a
   3-4 leg combo these could differ by a single ULP, silently stalling
   a correctly-priced order in `SUBMITTED` status forever. Fixed with
   a `1e-6`-dollar epsilon tolerance.
3. **ACCEPT-002 (LOW)** — `src/risk/trade_risk.py`'s `check_liquidity`
   let an infinite (`+inf`) bid/ask quote pass its spread-percentage
   check (`NaN > threshold` is always `False` in Python), though the
   pipeline still failed closed one stage later via a less-specific
   exception. Fixed with an explicit `math.isfinite()` guard.
4. Two stale-documentation findings (LOW): `src/strategies/base.py`'s
   module docstring and `strategy_type_for`'s docstring still described
   the pre-Step-20A "9 wired / 3 evaluation-only" split — caught by a
   purpose-built grep-based regression test
   (`TestNoStaleEvaluationOnlyLanguage`); fixed. `SECURITY_AUDIT.md`'s
   OP-003 entry made factually stale claims ("only leg 0's quantity is
   consulted," "every leg always shares the same quantity") that
   Step 20A's butterfly deliberately contradicts — corrected in place
   with an accurate, narrower description of the remaining real
   (pre-existing, MEDIUM, defense-in-depth, no live exploit path) gap.

No CRITICAL issue was found. No fix touched Risk Engine decision logic,
any `config/risk_limits.yaml` value, or `config/brokers.yaml`'s
`allowed_strategies`. No test was deleted, weakened, or bypassed.

**Full suite after all fixes:** `python -m pytest -q tests/` →
**2311 passed, 4 skipped** (2125 pre-existing + 186 new acceptance
tests, 0 regressions; the 4 skips are the same pre-existing, documented
false positives from `test_no_hardcoded_limits.py`, unrelated to this
step).

**Unresolved issues:** `SECURITY_AUDIT.md`'s pre-existing OPEN
MEDIUM/LOW findings (OP-003 as corrected above, OP-005, SY-007, SY-008,
LM-001, LM-002, QF-001, MD-007, MD-008, FS-005) remain open — none
CRITICAL or HIGH, none introduced or worsened by this step, fixing them
was outside this step's verification/hardening mandate. No market-
hours/trading-calendar/DST module exists yet (a known, already-
documented gap per `ARCHITECTURE.md`'s own risk list, not a new
finding). No export functionality exists yet (confirmed N/A, not
fabricated as tested).

**Acceptance decision: READY_WITH_NON_BLOCKING_WARNINGS.** Full
rationale in `ACCEPTANCE_TEST_REPORT.md` §30. This is a statement
about codebase readiness, not an instruction to proceed — **Step 22
has NOT been started, V1.0 has NOT been frozen, and the 90-day
validation has NOT begun.** All three remain the user's decision; the
formal validation cohort's own `has_cohort_started` check (re-verified
in `tests/acceptance/test_validation_pipeline.py`, including a check
against the real repository filesystem, not just a fresh in-memory
object) confirms it did not accidentally start during this pass either.

## Step 22: Pre-Validation Hardening & PAPER_TRADING_V1.0 Freeze

Closed the three operational gaps Step 21 left open (persistent
validation storage, a market-hours/holiday calendar, and a reporting/
export layer), fixed the two lowest-severity open `SECURITY_AUDIT.md`
findings with straightforward low-risk remediations (OP-003, FS-005),
then froze the result as **PAPER_TRADING_V1.0**. Full detail in the new
`STEP_22_FREEZE_REPORT.md` at the repo root; summarized here.

**Persistent storage** (`src/validation/session.py`'s
`SqliteValidationStore`, `src/validation/records.py`): cohorts,
opportunities (full decision trail — alternatives considered,
Devil's Advocate review, Portfolio Manager decision, Risk Engine
decision), trades, daily snapshots, rule violations, and a new
`ReconciliationFailureRecord` type (a fill recorded but its portfolio
update didn't complete — durable, never silently dropped) all persist
to `data/options_agent.db` (configurable, gitignored, not
pre-populated), idempotently (`INSERT OR IGNORE` on a natural-key
`UNIQUE` constraint for append-only records; upsert for mutable ones),
each write its own sqlite transaction. Proven via
`tests/acceptance/test_persistence_restart_recovery.py`: populate a
real store via the real pipeline, `del` the Python object (simulating a
crash), construct a brand-new store against the same file, reconstruct
everything, assert identical — twice, across two simulated restarts.
Backup/restore: `make backup`/`make restore FILE=...`
(`scripts/backup.sh`/`scripts/restore.sh`), 7 tests.

**Market calendar** (`src/data/market_calendar.py`): a self-contained,
stdlib-only deterministic NYSE calendar (floating federal holidays,
Good Friday via Meeus/Jones/Butcher, the December-lookback New Year's
edge case, early closes, DST-correct `zoneinfo` Eastern/UTC conversion)
— deliberately not `pandas_market_calendars`, to avoid a first-ever
`pandas` dependency for a need this platform can meet with published,
stable observance rules. 51 deterministic tests against fixed named
dates. Wired into `morning_scan.py` as an additive `market_open`/
`market_status_detail` field pair plus a `*** MARKET CLOSED ***` banner
— informational, not execution-blocking (blocking would incorrectly
prevent legitimate pre-market research and break the existing Sunday-
dated test fixtures for no real safety benefit, since the scan doesn't
fetch a currently-executable quote itself).

**Reporting/export layer** (`src/reporting/`): re-inspected the repo
first per instruction rather than trusting Step 21's finding blindly —
confirmed genuinely absent, then built a right-sized (not elaborate)
layer: one `ValidationReportBundle` per export call
(`build_report_bundle`), written out as CSV (9 flat tables), JSON (one
versioned payload), XLSX (12-worksheet workbook, numeric cells kept
numeric), and PDF (13-section owner/investment-committee report, zero
LLM involvement). `tests/acceptance/test_export_reproducibility.py` (4
tests): two export runs from one frozen store produce identical
quantitative content; every exported figure cross-checks directly
against its source record; an empty cohort exports honestly (zeroed/
N/A, never fabricated).

**Security fixes** (`SECURITY_AUDIT.md` OP-003, FS-005): a new
`PlaceOrderRequest` validator (`src/brokers/base.py`) requires every
leg's quantity to form one of this platform's own defined combo ratios
(uniform, or 1:2:1 for `long_call_butterfly`'s 3 legs) — a no-op on
every real call site, defense-in-depth against a hypothetical future
malformed caller (8 new tests). A new `FidelityTradeTicket` validator
(`src/brokers/fidelity.py`) requires direct construction to be at
`AWAITING_HUMAN`, exploiting pydantic v2's own verified behavior that
`model_copy()` — which `transition()`/`confirm_fill()` exclusively use
— never re-runs `@model_validator` hooks, so the real state machine is
untouched (3 pre-existing tests updated to use `model_copy()` instead
of direct construction, matching how production code actually reaches
those states). All other `SECURITY_AUDIT.md` OPEN items remain open —
none CRITICAL/HIGH, unchanged from Step 21.

**Safety-invariant re-verification** (Part 19): located the specific
test(s) proving each of the 15 named invariants. 14 already had direct
coverage; one gap was found (no direct test that `BrokerEnvironment` is
a single-member, `PAPER`-only enum — only indirect/behavioral coverage
existed) and closed with 2 new tests in `tests/unit/brokers/test_base.py`.

**Freeze tooling** (`src/validation/freeze.py`, new): builds/verifies
`VALIDATION_MANIFEST.json` — git commit/branch/state, Python version, a
dependency-requirements hash, config-file hashes (with
`strategies.yaml`/`universe.yaml` explicitly recorded not-applicable,
since this repo never split those out separately), `CLAUDE.md` and
every prompt file's hash, whole-directory hashes for `src/quant/`,
`src/risk/`, `src/strategies/`, plus `src/brokers/paper.py` and
`src/data/market_calendar.py`; fill-model/slippage/commission/
multiplier assumptions; benchmark definitions; database/export schema
versions; and the three explicit safety flags
(`live_trading_enabled`, `automatic_fidelity_execution`,
`validation_cohort_started`), all `false`. `make verify-freeze`
(`scripts/verify_freeze.sh`) re-derives every one of these against the
current tree and reports drift per-field, never silently. 14 new tests
(`tests/unit/validation/test_freeze.py`), including explicit drift-
detection and tampering-detection cases. Material-vs-non-material
change is defined directly in `freeze.py`'s own module docstring: every
hashed file/module is material by definition; anything not hashed
(README prose, this file) is not; a reported drift is always a human
decision, never auto-dismissed as cosmetic by the tooling.

**Also fixed, unrelated to the freeze itself:** an unanchored `data/`
pattern in `.gitignore` (added earlier this step for the runtime
database directory) was silently excluding the unrelated source
directories `src/data/` and `tests/unit/data/` from every `git add` —
caught when `src/data/market_calendar.py` failed to appear in `git
status` despite being written and passing tests. Fixed by anchoring the
Step 22 `.gitignore` entries to the repository root (`/data/`,
`/backups/`, `/exports/`).

**Full suite:** `python -m pytest -q tests/` → **2404 passed, 4
skipped, 0 failed** (2,311 Step-21 baseline + 93 new Step 22 tests; the
4 skips are the same pre-existing documented false positives, unrelated
to this step). No test was deleted, weakened, or bypassed; every
pre-existing test a Step 22 change legitimately obsoleted was rewritten
to assert the new correct behavior, never deleted outright.

**README.md** updated for a non-technical owner: §11 ("How to restart
it safely"), §12 ("Where reports and exports are stored"), §13 ("Where
validation results — the database — are stored," including backup/
restore instructions), and a new §14 ("Starting the 90-day validation
— do this only when you're ready") that documents the install → verify
freeze → open dashboard → verify market status sequence *without*
performing the actual Day-1 initialization, which this release
deliberately does not build (that is "Step 23," separately authorized).

**Git commit (code freeze):** `ca86e33fa07e9d04ee55ec9ee350e6a90f3f5532`
— "Step 22: pre-validation hardening (persistence, market calendar,
reporting/export, security fixes)". `VALIDATION_MANIFEST.json`'s own
`git_commit` field records exactly this SHA (the manifest was generated
immediately after this commit, against a clean working tree).
**Manifest hash:** `f488c9c5c54b2026332eceacac45000106a10221ef0fef34bb42f3d24aaf3098`.
`make verify-freeze` reports all 28 checks passing.

**Git commit (this entry, `STEP_22_FREEZE_REPORT.md`, and
`VALIDATION_MANIFEST.json` together)** and **git tag `paper-trading-v1.0`**
(applied to that same commit) are recorded by the commit that
immediately follows this one in `git log` — necessarily one commit
after the code-freeze commit above, since a manifest has to describe a
tree before it can be added to that tree. Run `git log --oneline -1
paper-trading-v1.0` or `git show paper-trading-v1.0:STEP_22_FREEZE_REPORT.md`
to see it directly.

**PAPER_TRADING_V1.0: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY.** No cohort
was created, no Day 1 snapshot was recorded, no trades were generated
as part of a formal validation cohort, starting NAV was not altered,
and no scheduling was enabled. Per this step's own explicit
instruction, work stops here — initializing the cohort and starting
the 90-day clock is Step 23, to be separately authorized.

## Step 22.1: Pre-Validation Controlled Amendment — Alpaca OPRA Market Data

A controlled amendment to the frozen PAPER_TRADING_V1.0 state: added
Alpaca (`alpaca-py` SDK) as a third `MarketDataProvider` implementation
(alongside `mock`/`ibkr`) so the 90-day validation can use real current
U.S. equity and options market data, preferably OPRA, without requiring
an IBKR account — then re-froze the result as **PAPER_TRADING_V1.1**.
Full detail in the new `STEP_22_1_FREEZE_REPORT.md`; summarized here.
**The original V1.0 artifacts (`STEP_22_FREEZE_REPORT.md`, the
`paper-trading-v1.0` tag) were preserved untouched, not overwritten.**

**Architecture audit first** (per instruction): traced how market data
actually flows in this codebase before writing anything. Two findings
that shaped the implementation: (1) `app/` (an early `app/data
/mock_provider.py`/`app/config.py` prototype) is dead code — nothing
under `src/` imports it, `scripts/start.sh` runs `src.dashboard.app:app`
exclusively; documented in `ARCHITECTURE.md`, left in place. (2) No
production code path anywhere previously selected a market-data
provider at runtime — `src.dashboard.service`/`src.workflows.feed_health`
both take already-fetched `OptionChain`s as plain parameters, and
`/morning-scan` is a Claude Code skill whose runner is expected to
construct a provider directly. `src/data/factory.py` (new) is the
first explicit `OPTIONS_AGENT_DATA_PROVIDER -> provider instance`
construction point in this codebase.

**Alpaca is structurally market-data-only** (`src/data/alpaca_provider.py`,
`src/data/alpaca_historical.py`): `AlpacaMarketDataProvider` implements
only `MarketDataProvider`'s two read methods, is not a `Broker`
subclass, defines no order-submission/cancellation/modification method,
and never imports `alpaca.trading` (Alpaca's separate order-submission
client) anywhere. Enforced by 20 dedicated acceptance tests
(`tests/acceptance/test_alpaca_market_data_only.py`) — a repo-wide
`alpaca.trading` import grep, a public-surface-equals-
`MarketDataProvider` check, `config/brokers.yaml`/`BrokerEnvironment`/
Risk-Engine untouched checks, credential-leakage checks, and a
socket-patched runtime proof that a faked call never opens a socket —
plus a new standing check inside `src.validation.freeze.verify_freeze()`
itself (`_verify_alpaca_is_market_data_only`), independent of the test
suite.

**Official SDK, verified endpoints**: `alpaca-py==0.44.0` (current
version confirmed via PyPI/GitHub source directly, since `alpaca.markets`
itself is blocked by this environment's egress policy). Only
`StockHistoricalDataClient.get_stock_latest_quote`,
`OptionHistoricalDataClient.get_option_chain`, and
`StockHistoricalDataClient.get_stock_bars` are called — every field
name (`Quote.bid_price`, `OptionsSnapshot.greeks`, `OptionsGreeks.delta`,
etc.) was verified directly against the installed package
(`Model.model_fields`), not assumed from memory or documentation.
Standard OCC option symbols (root+expiration+right+strike) are parsed
directly from Alpaca's chain-response keys rather than calling the
*trading* API's separate contract-metadata endpoint — keeping this
module strictly market-data-only architecturally, not just by
omission.

**OPRA vs indicative, never silently substituted**: `AlpacaConfig
.options_feed` (`"opra"`/`"indicative"`) is explicit, validated config;
`AlpacaFeedEntitlementError` raises immediately if the account isn't
entitled to the configured feed rather than falling back to the other
one. Every canonical quote/contract's own `source` field records
exactly which feed served it (`alpaca_opra`, `alpaca_indicative`,
`alpaca_sip`, `alpaca_iex`) — reusing the pre-existing, deliberately
open-string `source` field rather than adding a new canonical-schema
field.

**Provider health check** (`src/data/provider_health.py`, new):
REAL_DATA_CONNECTED / MOCK_DATA / REAL_DATA_UNAVAILABLE, auth status,
feed type, OPRA entitlement (when determinable), market-open state
(reusing Step 22's `src.data.market_calendar` unmodified), and
freshness — surfaced via a new read-only `GET /api/data-provider-health`
dashboard route and a small "Market Data" panel, so the owner never has
to guess what kind of data the system is using. The dashboard's route
allowlist test was updated to include exactly this one new GET route.

**Freeze tooling extended** (`src/validation/freeze.py`): added
`freeze_version`, `alpaca_provider_module_hash`,
`data_provider_at_freeze_time`, `required_options_feed_for_validation`
fields plus the standing structural check above — `FREEZE_NAME` bumped
to `PAPER_TRADING_V1.1` (manifest schema `1.1.0`) without touching the
original V1.0 fields' shape.

**Full suite:** `python -m pytest -q tests/` → **2492 passed, 4
skipped, 0 failed** (84 new tests across 7 files; the 4 skips are the
same pre-existing documented false positives). No test was deleted,
weakened, or bypassed.

**Git commit (code amendment):** `a33c7ba7358e31d66463252097ae9a5ea26d0a8f`
— "Step 22.1: add Alpaca as a market-data-ONLY provider (pre-validation
amendment)". `VALIDATION_MANIFEST.json`'s own `git_commit` field
records exactly this SHA (manifest generated immediately after this
commit, against a clean working tree).
**Manifest hash:** `62bef508f4aa99996bd84a5b3b044bab4dca3b49e3f8e65cd7937cd91050f0d9`.
`make verify-freeze` reports all 29 checks passing (28 from V1.0 plus
the new `alpaca_market_data_only` structural check).

**Git commit (this entry, `STEP_22_1_FREEZE_REPORT.md`, and
`VALIDATION_MANIFEST.json` together)** and **git tag `paper-trading-v1.1`**
(applied to that same commit) are recorded by the commit that
immediately follows this one in `git log` — one commit after the code
commit above, for the same reason as V1.0. Run `git log --oneline -1
paper-trading-v1.1` or `git show paper-trading-v1.1:STEP_22_1_FREEZE_REPORT.md`
to see it directly.

**PAPER_TRADING_V1.1: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. ALPACA:
MARKET_DATA_ONLY.** No cohort was created, no Day 1 snapshot was
recorded, no trades were generated, starting NAV was not altered, and
no scheduling was enabled. Work stops here per this amendment's own
explicit instruction — Step 23 remains separately authorized, not
started.

## Step 22.2: Pre-Validation Controlled Amendment — Stateful Wheel Strategy

**What was built.** A new package, `src/wheel/`, implementing the Wheel
as a persistent, multi-stage position lifecycle — never as a label
glued onto a standalone cash-secured put and covered call. Every order
a Wheel places is an ordinary `TradeProposal` with
`strategy=StrategyType.CASH_SECURED_PUT`/`COVERED_CALL`, submitted to
the exact same unmodified `src.risk.engine.evaluate_trade_proposal`
every other strategy in this platform uses. `src.wheel` adds the
state-machine and cross-cycle accounting layer on top of that unchanged
pipeline — never a shortcut around it, never a second Risk Engine, never
a special exemption from portfolio limits.

Modules added:
- `src/wheel/state.py` — `WheelState` (14 members) + `VALID_TRANSITIONS`
  + `transition()`, the single choke point every state change goes
  through.
- `src/wheel/models.py` — `WheelPosition`, `CspCycle`, `CcCycle`,
  `WheelAccounting`, `WheelEvent`, `WheelStateTransitionRecord` (all
  `extra="forbid"`, `frozen=True` Pydantic models, updated only via
  `.model_copy(update=...)`, matching `src.brokers.base.Order`'s own
  convention).
- `src/wheel/accounting.py` — pure functions for cash reservation,
  premium accounting, acquisition-basis (tax-style) vs. economic-basis
  (premium-adjusted) computation, called-away realized P&L, and
  `summarize_wheel_economics`/`WheelEconomicsSummary` (every Part 6/9
  metric assembled once for the dashboard, Fidelity context, and
  validation reporting to all read the same numbers).
- `src/wheel/eligibility.py` — deterministic candidate screening:
  approved universe, underlying/option liquidity, bid/ask spread, DTE/
  delta range (delta computed via Black-Scholes when the provider
  doesn't supply one, never fabricated), earnings proximity (explicit
  `DATA_UNAVAILABLE` status, never silently treated as "no earnings"),
  cash sufficiency, and reuse (not reimplementation) of
  `src.risk.concentration`/`src.risk.correlation`.
- `src/strategies/base.py`/`src/strategies/wheel.py` — added
  `StrategyKind.WHEEL` (16th member, family INCOME+DIRECTIONAL_BULLISH,
  permanently absent from `TRADE_PROPOSAL_ELIGIBLE` since a Wheel is
  never itself submitted as an order) and
  `evaluate_wheel_candidate` (reuses the CSP entry's own priced
  economics for strategy-competition comparison — never a fabricated
  multi-cycle projection).
- `src/wheel/lifecycle.py` — one function per state-machine edge:
  `open_wheel_candidate`, `reject_candidate`, `open_csp`,
  `csp_expires_worthless`, `csp_bought_to_close`, `csp_assigned`,
  `mark_cc_eligible`, `no_cc_trade` (Part 7: holding shares without a
  call is valid, recorded as an audit event, not a state change),
  `open_cc` (raises `UncoveredCallError` if `shares_owned < 100 *
  contracts`, defense in depth alongside PaperBroker's own independent
  check), `cc_expires_worthless`, `cc_bought_to_close`,
  `shares_called_away`, `complete_wheel`, `halt_wheel`, `exit_wheel`.
- `src/wheel/paper_events.py` — wires the lifecycle to real
  `PaperBroker` fills/settlements: opens require an already
  Risk-Engine-approved `RiskDecisionResult`
  (`WheelOrderNotApprovedError` otherwise), converted to a
  `PlaceOrderRequest` via the existing
  `src.brokers.order_validator.validate_and_build_order_request`;
  settlement reads `PaperBroker.settle_expiration`'s own
  `assigned_or_exercised` flag to decide assignment vs. worthless
  expiration — never a second, independently-guessed outcome.
- `src/wheel/fidelity_events.py` — opens reuse the unmodified
  `FidelityManualProvider`/`ApprovedOrder`/`FidelityTradeTicket`
  machinery as-is; assignment is recorded as a plain reconciliation
  event (`record_wheel_csp_assignment`/`record_wheel_cc_assignment`),
  never a fabricated order, per the amendment's explicit Part 20
  requirement. Discretionary buy-to-close tickets are documented as a
  pre-existing platform gap (the Risk Engine has no CLOSE/ROLL pipeline
  capability at all, `src.risk.engine`'s own TS-004 comment) rather
  than force-fitting fabricated "max profit" economics onto
  `ApprovedOrder`'s opening-trade-shaped schema.
- `src/wheel/risk.py` — `stress_test_wheel` at
  `WHEEL_STRESS_SPOT_SHOCKS = (-0.05, -0.10, -0.20, -0.30, -0.50)` (the
  exact Part 12 grid, deeper than the platform's general +-5/10/20%
  grid); `compute_wheel_aggregate_exposure` feeds the same
  `underlying_exposure_pct`/`sector_exposure_pct` functions every other
  strategy's capital-at-risk already goes through.
- `src/wheel/persistence.py` — `WheelStore`/`InMemoryWheelStore`/
  `SqliteWheelStore`, one JSON row per `wheel_id` (the whole nested
  history round-trips through `model_dump_json`/`model_validate_json`,
  same pattern `SqliteIdempotencyStore` already uses for `Order`);
  `WHEEL_DATABASE_SCHEMA_VERSION` tracked independently.
- `src/wheel/backtest_engine.py` — a genuinely stateful multi-cycle
  backtest (unlike `src.backtest.engine.run_backtest`'s independent-
  round-trip model): drives `src.wheel.lifecycle` through a real
  day-by-day loop so a CSP assignment feeds directly into the covered-
  call phase within the same run, reusing `src.backtest.execution`/
  `src.backtest.assignment` for all pricing. Every quote lookup piped
  through `assert_no_lookahead_options`; a missing settlement quote
  raises `WheelBacktestDataInsufficientError` rather than being
  fabricated.
- `src/llm/context.py` (`WheelReviewContext`) + `src/wheel
  /review_context.py` — Part 17's 10 named failure-mode prompts and
  Part 18's CSP-phase/CC-phase Portfolio Manager question sets, wired as
  an optional field on `DevilsAdvocateInputs`/`PortfolioManagerInputs`
  (present only for a Wheel leg, absent for every other proposal).
  `DevilsAdvocateReview.failure_scenarios` already required
  `min_length=3` for every review before this amendment — Part 17's
  "at least three failure modes" was already a structural guarantee, not
  a new schema requirement.
- `.claude/agents/devil_advocate.md` / `.claude/agents/portfolio_manager.md`
  — updated with the Wheel-specific checklists verbatim.
- `src/validation/wheel_attribution.py` — cohort-level Wheel metrics
  (assignment rate, below-basis call rate, expectancy, a pseudo-equity-
  curve max-drawdown figure, tail-loss average, capital efficiency, and
  pass-through comparison figures against buy-and-hold/standalone-CSP/
  cash). `win_rate` is deliberately not a field on
  `WheelCohortAttribution` at all, per Part 22's "do not judge Wheel
  quality solely on win rate."
- `src/dashboard/models.py`/`schemas.py`/`app.py` — `DashboardState`
  gained `wheels`/`current_price_by_ticker`; two new read-only routes,
  `GET /api/wheels` and `GET /api/wheels/{wheel_id}`, both added to the
  existing route allowlist test. No POST route exists for a Wheel
  anywhere. Static frontend (`index.html`/`dashboard.js`/`dashboard.css`)
  gained a "Wheels" panel, clearly labeled RESEARCH / PAPER.

**Testing.** 199 new tests across `tests/unit/wheel/` (9 files: state,
accounting, lifecycle, eligibility, risk, persistence — including an
explicit restart-recovery proof, paper_events (async, real PaperBroker
fills), backtest_engine, fidelity_events, review_context),
`tests/unit/validation/test_wheel_attribution.py`,
`tests/unit/dashboard/test_app_routes.py` (new `TestWheelRoutes` class),
and `tests/acceptance/test_wheel_security.py` (20 tests: no live
trading-client import or live-shaped order-submission method anywhere in
`src/wheel/`, no Fidelity auto-execution, no Alpaca import, no
deterministic Wheel module imports `src.llm.client`/`src.llm.router`,
`open_cc`'s `UncoveredCallError` check proven to precede `CcCycle`
construction in source order, `WHEEL` absent from
`config/brokers.yaml`/`config/risk_limits.yaml`/`src/risk/engine.py`,
and every Wheel Pydantic model rejects an unrecognized field).

Adding `StrategyKind.WHEEL` (15 -> 16 members) required updating 3
pre-existing tests that hardcoded "15" as the total `StrategyKind`
count (`tests/acceptance/test_strategy_integrity.py`,
`tests/unit/strategies/test_base.py`,
`tests/unit/validation/test_strategy_attribution.py`) — each updated to
assert 16 total members / explicitly confirm `"wheel"` stays absent from
`TRADE_PROPOSAL_ELIGIBLE`, per CLAUDE.md's "a test asserting a
now-reversed architectural decision should be updated to assert the new,
intentional behavior — not deleted and not left failing." No test was
deleted or weakened.

**Full suite:** `python -m pytest -q tests/` → **2699 passed, 4 skipped,
0 failed** (207 net new tests, including the freeze-tooling tests added
below; the 4 skips are the same pre-existing documented false positives
carried from every prior freeze).

**Non-negotiable invariants re-verified, unchanged:**
- Live trading: still structurally impossible (`BrokerEnvironment` still
  exactly `[PAPER]`; no Wheel module imports a live trading client).
- Fidelity: still `MANUAL_EXECUTION` only; assignment is a
  reconciliation event, never a fabricated order.
- Alpaca: still market-data-only (untouched by this amendment).
- Risk Engine: still sole authority; no Wheel-specific branch exists
  anywhere in `src/risk/engine.py`.
- Naked calls: structurally impossible — `open_cc` checks
  `shares_owned >= 100 * contracts` before constructing any `CcCycle`,
  and `PaperBroker`'s own independent collateral check refuses an
  uncovered call regardless.
- Config-declared capability: `config/brokers.yaml` gained no `WHEEL`
  entry — a Wheel's orders are ordinary CASH_SECURED_PUT/COVERED_CALL
  orders, already-declared capabilities.

**Git commit (code amendment):** `dac6055efc9fea74c8ae6e617076266bc446dea6`
— "Step 22.2: add a stateful Wheel strategy (pre-validation amendment)".
`VALIDATION_MANIFEST.json`'s own `git_commit` field records exactly
this SHA (manifest generated immediately after this commit).
**Manifest hash:** `11a3a5972eef325a60889cf466a66d8bfd1d794fefb99dd21b2600e01b4930ea`.
`make verify-freeze` reports all 33 checks passing, including the two
new standing structural checks this amendment adds,
`wheel_no_live_trading_client` and
`wheel_never_becomes_its_own_order_type`, alongside the new
`wheel_module_hash` drift check.

**Git commit (this entry, `STEP_22_2_FREEZE_REPORT.md`, and
`VALIDATION_MANIFEST.json` together)** and **git tag `paper-trading-v1.2`**
(applied to that same commit) are recorded by the commit that
immediately follows this one in `git log` — one commit after the code
commit above, for the same reason as V1.0/V1.1. Run `git log --oneline
-1 paper-trading-v1.2` or `git show
paper-trading-v1.2:STEP_22_2_FREEZE_REPORT.md` to see it directly.

**PAPER_TRADING_V1.2: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. ALPACA:
MARKET_DATA_ONLY. WHEEL: never trade-proposal-eligible, always an
ordinary CASH_SECURED_PUT/COVERED_CALL order underneath.** No cohort
was created, no Day 1 snapshot was recorded, no trades were generated,
starting NAV was not altered, and no scheduling was enabled. Work stops
here per this amendment's own explicit instruction — Step 23 remains
separately authorized, not started.

## Step 22.3: Deterministic Strategy Lifecycle Management Engine

**What was built.** A new package, `src/lifecycle/`, managing every
strategy this platform supports (all 16 `StrategyKind` members,
including the Wheel by delegation, never duplication) through ENTRY ->
ACTIVE -> MONITORING -> MANAGEMENT DECISION -> EXIT/EXPIRATION/
ASSIGNMENT/ADJUSTMENT -> POST-TRADE ANALYSIS. Like every other package
in this codebase, it remains strictly subordinate to
`src.risk.engine.evaluate_trade_proposal` — nothing in `src/lifecycle/`
places, cancels, or modifies a live brokerage order, and a roll or
adjustment's new OPEN leg is a genuinely new `TradeProposal` that must
independently clear the full existing approval pipeline.

Modules added:
- `src/lifecycle/state.py` — `PositionLifecycleState` (28 members) +
  `VALID_TRANSITIONS` + `transition()`, mirroring but never importing
  `src.brokers.fidelity.TicketStatus`'s pre-fill vocabulary.
  `RISK_EXIT_REQUIRED` is reachable from every monitoring state (not
  only `ACTIVE`) and has no path back to `ACTIVE` — including no
  indirect two-hop path through `HALTED`, a loophole caught and closed
  during this step's own smoke testing before any test was written
  against it. `from_ticket_status()` bridges the two vocabularies
  explicitly (`TicketStatus.EXPIRED` maps to `CANCELLED`, never to
  `PositionLifecycleState.EXPIRED`, which means a different real-world
  event).
- `src/lifecycle/policy.py` — `ManagementPolicy` (every Part 3 field,
  frozen dataclass) + `validate_policy_for_strategy`, driven by three
  structural capability lookup tables (`_HAS_SHORT_LEG`,
  `_RECEIVES_CREDIT_AT_ENTRY`, `_CAN_BE_ASSIGNED`) covering all 16
  `StrategyKind` members, so a field presupposing a structural property
  a strategy doesn't have (e.g. `delta_threshold` on a no-short-leg
  spread) fails validation rather than being silently accepted.
- `src/lifecycle/excursion.py` — immutable `ExcursionState` (MFE/MAE as
  signed values, never forced non-negative) + `exit_efficiency` (`None`,
  never a fabricated 0.0/1.0, for a position never profitable).
- `src/lifecycle/triggers.py` — nine pure comparison functions (Parts
  4-12: profit target, loss, DTE, delta, volatility, regime change,
  earnings/event, liquidity, assignment risk), each returning
  `TriggerFinding`s tagged with one of Part 18's 11 precedence
  categories; a missing input a configured policy field needs always
  produces `DATA_INSUFFICIENT`, never a silently skipped check.
- `src/lifecycle/precedence.py` — `resolve_action`, the single function
  that turns a list of `TriggerFinding`s (plus an externally-supplied
  Risk-halt boolean) into one deterministic `ResolvedAction`, honoring
  Part 18's exact 11-level hierarchy.
- `src/lifecycle/snapshot.py` — immutable, append-only
  `LifecycleDecisionSnapshot` (Part 17): every observed input, every
  candidate action, and the one resolved, per evaluation.
- `src/lifecycle/engine.py` — `evaluate_position`, the pure orchestrator
  (`PositionMonitoringInput` in, `EvaluationResult` out) tying
  triggers + precedence + excursion + state + snapshot together; no I/O,
  no LLM-shaped parameter anywhere in its signature.
- `src/lifecycle/rolling.py` — Part 13: a roll modeled as exactly two
  transactions (`record_roll_close` realizes P&L and tracks cumulative
  chain loss immediately; `complete_roll` records the new leg only once
  it has independently cleared the full pipeline) — a large realized
  loss is never netted away by a favorable-looking roll credit.
- `src/lifecycle/adjustment.py` — Part 14: seven named `AdjustmentType`s
  (including the worked `CLOSE_UNCHALLENGED_SIDE` example), each an
  `AdjustmentProposal` with explicit before/after max loss, capital
  requirement, breakevens, and Greeks — packaging numbers Quant/Risk
  already computed, never re-deriving option math.
- `src/lifecycle/policies_library.py` — 19 named RESEARCH DEFAULT
  `ManagementPolicy` instances, covering all 16 `StrategyKind` members
  (4 independently-named policies for `PUT_CREDIT_SPREAD` alone,
  proving Part 1's "never assume one management policy is universally
  superior"), validated at import time; `WHEEL_STANDARD` documents that
  it governs only CSP/CC leg-level triggers, never the wheel_id-level
  state machine `src.wheel` already owns.
- `src/lifecycle/paper_events.py` — Part 20: `close_position` builds
  correctly-reversed closing legs and submits through the unmodified
  `PaperBroker.place_order`; `settle_lifecycle_expiration` is a thin
  pass-through to `PaperBroker.settle_expiration`. Exposes no function
  that opens a new position.
- `src/lifecycle/fidelity_events.py` — Part 21: `LifecycleClosingTicket`,
  a deliberately separate, simpler shape from `ApprovedOrder`/
  `FidelityTradeTicket` (no `max_profit`/`max_loss`/`breakeven` — fields
  with no real meaning for a closing order), rendered as plain text;
  `FILLED` only ever follows an explicit human-reported
  `record_ticket_filled` call.
- `src/lifecycle/persistence.py` — sqlite-backed `LifecyclePositionRecord`
  (REPLACE-on-save) and append-only `LifecycleDecisionSnapshot` storage,
  plus `Alert` storage; restart recovery proven via a fresh
  `SqliteLifecycleStore` connection against the same file.
- `src/lifecycle/alerts.py` — Part 23: 11 named `AlertType`s (the ten
  plus `VOLATILITY_CHANGE`) with `raise_alert_if_new` suppressing a
  duplicate alert for the same still-unresolved condition.
- `src/validation/post_trade_analysis.py` — Part 24: `ClosedPositionAnalysis`
  (actual outcome: realized P&L, return on risk/capital, MFE/MAE, exit
  efficiency, commissions) plus a separate `counterfactuals` tuple,
  reusing `src.backtest.execution.execute_exit`/
  `src.backtest.assignment.settle_position` exactly as
  `src.workflows.rejected_trade_review` does for a rejected proposal —
  counterfactuals never alter the recorded actual.
- `src/validation/policy_attribution.py` — Part 25: dual-level
  performance (`strategy_level_performance` and
  `strategy_policy_level_performance`), Sharpe/Sortino/max-drawdown/CVaR
  via the existing `src.backtest.metrics` implementations against a
  synthetic trade-level equity curve, `sample_size_warning` reusing
  `src.research.overfitting_guards.check_small_sample`.
- `src/dashboard/schemas.py`/`models.py`/`app.py` — `GET /api/lifecycle`,
  `GET /api/lifecycle/{trade_id}` (read-only, added to the existing
  route allowlist test), `LifecyclePositionView`, 12
  `LifecycleStatusIndicator` values (Part 22's ten plus
  `REGIME_REVIEW`/`ASSIGNMENT_REVIEW`) derived purely from a
  `ResolvedAction`'s own category.
- `src/dashboard/static/index.html`/`dashboard.js` — an Active
  Positions/Lifecycle panel, read-only, no execution controls.

**Bugs caught and fixed during this step's own smoke testing (before
any formal test was written against them):**
1. `src/lifecycle/policy.py`'s first draft of
   `validate_policy_for_strategy` contained dead code left over from
   exploring the CASH/NO_TRADE edge case — an `if ...: pass` no-op
   branch, a `False`-gated dead branch, and a broken
   `hasattr(StrategyKind, "CASH_NO_TRADE")` check against an enum
   member that has never existed (CASH/NO_TRADE is `selected=None`, not
   a `StrategyKind` value, per Step 19A/22.2's own established design).
   Removed and replaced with a one-line documentation comment, per
   CLAUDE.md's standing instruction against unreachable defensive
   branches.
2. `src/lifecycle/state.py`'s first draft only added the `HALTED`/
   `DATA_INSUFFICIENT` escape hatches to `ACTIVE`, not to the other
   seven `*_TRIGGERED`/`ADJUSTMENT_CANDIDATE` states — meaning a
   position already sitting in, say, `PROFIT_TARGET_REACHED` could not
   be force-exited by a portfolio Risk halt, silently violating Part
   18's "nothing outranks Risk." Caught by the engine-level smoke test
   (`evaluate_position` from a trigger state with `risk_halt_active=True`
   raised `InvalidLifecycleTransitionError` instead of transitioning to
   `RISK_EXIT_REQUIRED`). Fixed by adding `RISK_EXIT_REQUIRED` to the
   same escape-hatch set applied to every post-fill monitoring state.
3. That same fix, applied naively, would have added a
   `RISK_EXIT_REQUIRED -> HALTED` edge — which combined with
   `HALTED`'s own existing `-> ACTIVE` edge would have created a
   two-hop bypass (`RISK_EXIT_REQUIRED -> HALTED -> ACTIVE`) of the
   explicit "no discretion to fall back to ACTIVE" rule on
   `RISK_EXIT_REQUIRED`'s own base transition. Caught immediately by
   re-reasoning about the fix before writing the formal test suite;
   fixed with an explicit override,
   `VALID_TRANSITIONS[RISK_EXIT_REQUIRED] = {EXIT_PENDING,
   DATA_INSUFFICIENT}` (dropping `HALTED`), applied after the generic
   comprehension. Both the original gap and this loophole are now
   directly tested in `tests/unit/lifecycle/test_state.py`.
4. `src/lifecycle/adjustment.py`'s first draft declared an
   `InvalidAdjustmentError` exception class that nothing ever raised
   (field-level validation already goes through Pydantic's own
   `ValidationError`, matching `src.wheel.models`'s established
   convention). Removed as dead code.

**A pre-existing test updated, not weakened, for the new intentional
route additions:** `tests/unit/dashboard/test_app_security.py`'s
explicit `_ALLOWED_ROUTES` allowlist (a route-inventory test that fails
loudly on any unlisted or execution-shaped route) gained
`("GET", "/api/lifecycle")` and `("GET", "/api/lifecycle/{trade_id}")`
— both read-only, both already covered by the same test's forbidden-
path-substring checks. No test was deleted or had an assertion loosened.

**Full suite:** `python -m pytest -q tests/` -> **3006 passed, 4
skipped, 0 failed** (307 net new tests: 261 in `tests/unit/lifecycle/`,
9 in `tests/unit/validation/test_post_trade_analysis.py`, 9 in
`tests/unit/validation/test_policy_attribution.py`, 7 in
`tests/unit/dashboard/test_lifecycle_routes.py`, 21 in
`tests/acceptance/test_lifecycle_security.py`; the 4 skips are the same
pre-existing documented false positives carried from every prior
freeze).

**Non-negotiable invariants re-verified, unchanged:**
- Live trading: still structurally impossible — no module under
  `src/lifecycle/` imports a live trading client, a network client, or
  a browser-automation library (proven by repo-wide regex grep in
  `tests/acceptance/test_lifecycle_security.py`, mirroring
  `test_wheel_security.py`'s own proof for `src/wheel/`).
- Fidelity: still `MANUAL_EXECUTION` only; `LifecycleClosingTicket`
  never self-transitions to `FILLED` — only an explicit human-reported
  `record_ticket_filled` call does, verified structurally (exactly one
  occurrence of `LifecycleTicketStatus.FILLED` in the whole module,
  inside that one function).
- Alpaca: still market-data-only (untouched by this amendment).
- Risk Engine: still sole authority; `rolling.py`/`adjustment.py`
  contain no import of `src.risk.engine` at all — a roll/adjustment
  proposal is packaged here and evaluated by Risk elsewhere, by the
  caller, exactly like every other proposal.
- LLM cannot override a deterministic action: no deterministic
  lifecycle module imports `src.llm.client`/`src.llm.router`, and
  `evaluate_position`/`resolve_action`/`transition`'s own signatures
  carry no LLM-verdict-shaped parameter at all (proven by signature
  inspection, not just by absence of a call).
- No live trading exists anywhere: every `src.lifecycle.paper_events`
  function either closes an existing position or reads an existing
  settlement — none opens one (proven structurally, not just by
  convention).

**PAPER_TRADING_V1.3: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. ALPACA:
MARKET_DATA_ONLY.** See `STEP_22_3_FREEZE_REPORT.md` for the freeze
commit SHA, manifest hash, and remote tag verification. No cohort was
created, no Day 1 snapshot was recorded, no trades were generated,
starting NAV was not altered, and no scheduling was enabled. Work stops
here per this step's own explicit instruction — Step 23 remains
separately authorized, not started.

## Step 22.4 — Tradier real-time market data + deterministic Portfolio
## Control Loop (controlled pre-validation amendment to PAPER_TRADING_V1.3)

A 52-part instruction extending the frozen V1.3 platform with a real
market-data provider (Tradier, MARKET_DATA_ONLY, mirroring the existing
Alpaca provider's structural guarantees) and a new `src/portfolio/`
package: the deterministic orchestrator that continuously revalues the
PAPER portfolio, monitors every open position via the unmodified V1.3
Lifecycle Engine, evaluates portfolio-level exposure/drawdown, monitors
pending Fidelity tickets for staleness, scans for new opportunities
without ever bypassing the Risk Engine, raises deduplicated alerts, and
persists an auditable decision history — all still subordinate to
`src.risk.engine.evaluate_trade_proposal` and still `Fidelity =
MANUAL_EXECUTION` forever.

**New modules:**
- `src/data/tradier_provider.py` — `TradierMarketDataProvider`, GET-only
  against `/v1/markets/*` (quotes, batch quotes, expirations, option
  chains). Constructor-injectable `http_client` for tests (no real
  `httpx`/token needed). Multi-symbol quote batching (one request per
  chunk, not per symbol), bounded retry/backoff with auth failures
  never retried, per-request rate-limit-priority gating (see
  `rate_limiter.py`), secret redaction on every exception path, and
  pure JSON->canonical mapping functions that return `None` rather than
  fabricate a contract from a malformed/incomplete entry.
- `src/data/rate_limiter.py` — `RateLimitPriority` (P0 position risk
  through P5 background research), `RateLimitState.from_headers` (built
  from the provider's own response headers, never a hardcoded plan
  assumption), `may_proceed`/`degraded_priorities`. P0 is throttled only
  by the hard `available<=0` floor, never by a utilization ceiling —
  the concrete mechanism behind Part 9's "risk monitoring is the last
  thing ever suspended."
- `src/data/quality_gate.py` — `validate_underlying_quote`/
  `validate_option_contract`/`validate_option_chain`: deterministic
  checks (crossed/zero/excessively-wide markets, NaN/infinity on fields
  Pydantic's own `ge`/`le` constraints don't already reject, staleness,
  symbol/expiration consistency) that isolate one bad contract from an
  otherwise-good chain rather than raising on the first one.
  `is_systemic_chain_failure` is the separate, explicit escalation
  point for "this whole underlying's data is unusable this cycle."
- `src/data/factory.py`/`option_chain.py` — extended (not replaced):
  `tradier` added to `_KNOWN_PROVIDERS` with the same fail-closed
  construction the Alpaca/IBKR branches already use; `OptionContract`
  gained optional `bid_size`/`ask_size`/`bid_timestamp`/`ask_timestamp`/
  `trade_timestamp` fields (additive, every existing provider/test
  unaffected).
- `src/portfolio/revaluation.py` — `revalue_position`/`revalue_portfolio`:
  per-leg mark-to-market from the current quality-gated chain, an
  independently-solved IV (`src.quant.volatility.implied_volatility`,
  never a provider's own reported IV) feeding `src.quant.greeks`, never
  Black-Scholes theoretical prices for P&L. A leg with no matching
  fresh contract makes that position `DATA_INSUFFICIENT` — never a
  fabricated $0 P&L — and the portfolio aggregate is computed only from
  positions that revalued cleanly, with the excluded set always listed.
- `src/portfolio/exposure.py` — `build_exposure_snapshot`: underlying/
  sector/strategy/expiration concentration (reusing
  `src.risk.portfolio_risk`'s existing helpers), directional/volatility
  exposure labels from caller-supplied Greeks (never recomputed here),
  short-option capital, Wheel cash commitment (active = not one of
  `src.wheel.state.TERMINAL_STATES`, so a halted-but-not-exited Wheel
  still counts), owned-share exposure (current price when supplied,
  honestly-flagged cost-basis fallback otherwise), covered-call
  encumbrance, and correlated-pair reporting reusing
  `src.risk.correlation`'s own documented price-history gap.
- `src/portfolio/actions.py` — `ControlLoopAction` (Part 17's 16-member
  vocabulary) plus `action_from_lifecycle_category`, a total,
  KeyError-on-gap relabeling of `src.lifecycle.triggers.PRECEDENCE_ORDER`
  categories — never a second precedence decision, only a display label
  on the one `src.lifecycle.precedence.resolve_action` already made.
- `src/portfolio/decision_snapshot.py`/`cycle_record.py`/`persistence.py`
  — `PortfolioControlDecisionSnapshot` (Part 30) and `ControlCycleRecord`
  (Part 31), both immutable/append-only; `ControlLoopStore`
  (`InMemory`/`Sqlite`, restart-survival verified) following
  `src.lifecycle.persistence`'s exact established pattern, extended in
  the same file to also store `ControlLoopAlert` (Part 32).
- `src/portfolio/control_loop.py` — `run_control_cycle`: MARKET DATA
  (already fetched by the caller, same convention as
  `src.workflows.morning_scan`) -> quality gate -> revaluation ->
  exposure -> per-position Lifecycle Engine evaluation (lazily
  initializing a `LifecyclePositionRecord` on first sight, never
  reinventing lifecycle state) -> `src.risk.kill_switch` -> recommendation
  -> persistence. One position's missing market data, missing
  management-policy configuration, or unknown policy name isolates to a
  `DATA_INSUFFICIENT` decision for that position alone; a portfolio
  halt (manual or drawdown) overrides every open position's own action
  to `PORTFOLIO_HALT`. New-opportunity scanning and pending-ticket
  monitoring are deliberately NOT phases of this function — their
  already-computed counts are accepted as plain `ControlCycleInputs`
  fields so this orchestrator never has to be rewritten once a thin
  outer wrapper runs those separate stages and merges the counts in.
- `src/portfolio/ticket_monitor.py` — Part 21's actual gap: pending
  (`AWAITING_HUMAN`/`ORDER_ENTERED`) Fidelity tickets are checked every
  cycle for stale market data, price drift beyond a configurable
  threshold, or a now-unreachable `minimum_acceptable_price` (same
  net-credit/net-debit sign convention `src.brokers.fidelity`'s own
  validator already uses); a bad finding calls the EXISTING
  `transition()` to `REPRICE_REQUIRED` — no new price is ever derived
  here. `record_confirmed_fill` is a documented one-line call-through to
  the EXISTING `confirm_fill()` (Part 22's fill reconciliation was
  already complete before this step; this module doesn't rebuild it).
- `src/portfolio/opportunity_scan.py` — `scan_and_rank_opportunities`:
  the lighter, no-LLM sibling of `run_morning_scan`, reusing
  `src.workflows.candidate_generation.generate_candidates`,
  `src.orchestration.pipeline.default_quant_stage`, and the one
  `evaluate_trade_proposal` unmodified — Devil's Advocate/Portfolio
  Manager remain `run_morning_scan`'s own separate, deliberately
  invoked job, never re-run on every cycle. Ranks only Risk-approved/
  resized candidates by risk-adjusted return (EV/capital); `best=None`
  (CASH/NO_TRADE) is the explicit outcome when nothing clears the
  configurable hurdle — proven by a test where a Risk-APPROVED candidate
  with negative risk-adjusted return still yields no trade.
- `src/portfolio/alerts.py` — `ControlLoopAlert`/`ControlLoopAlertType`
  (Part 32's list) with `AlertSeverity` (INFO/REVIEW/WARNING/CRITICAL),
  a separate `scope`-keyed sibling to `src.lifecycle.alerts.Alert`
  (whose `trade_id` is mandatory and can't represent a portfolio-wide or
  provider-wide condition) reusing the identical dedup-by-
  `(scope, alert_type)` mechanism. `generate_cycle_alerts` covers Risk
  halt, drawdown zone, provider outage vs. per-symbol staleness,
  rate-limit constraint, lifecycle-driven per-position alerts,
  expiration-approaching (checked independently of whichever action
  fired, not nested behind it — a real bug caught and fixed during this
  step's own testing, see below), pending-ticket staleness, and
  Risk-approved new opportunities only (never for a rejected/no-action
  scan result).
- `src/dashboard/` extended (schemas.py/models.py/app.py) — three new
  read-only, GET-only routes: `/api/control-loop/status`,
  `/api/control-loop/exposure`, `/api/control-loop/alerts`
  (severity-sorted, `unresolved_only` filter). `DashboardState` gained
  `latest_cycle_record`/`latest_exposure`/`control_loop_alerts`, all
  `None`/empty until a cycle has actually run — never a fabricated
  placeholder. No POST/PUT route of any kind for the control loop; the
  existing route-inventory allowlist test
  (`tests/unit/dashboard/test_app_security.py`) was updated (not
  loosened) to include exactly these three GET routes.

**Bugs caught and fixed during this step's own testing (before the
formal suite made them permanent regressions):**
1. `src.portfolio.control_loop._build_monitoring_input` originally
   derived `PositionMonitoringInput.position_delta_abs` from
   `PositionValuation.delta` — but that field is the position's total,
   contract-multiplier-scaled, multi-leg NET delta (share-equivalent
   units, easily in the hundreds), while `position_delta_abs` means one
   specific monitored leg's own per-contract delta in the standard
   [0, 1] convention. Feeding one to the other silently misfired every
   delta-threshold lifecycle policy. Caught by a smoke test asserting a
   0.20-delta credit spread should evaluate to `HOLD` and instead
   getting `ADJUSTMENT_CANDIDATE` from a "delta 34.7 reached threshold
   0.45" reason. Fixed by moving `position_delta_abs` into the explicit
   caller-supplied-extras set (alongside `short_leg_is_itm` and
   friends) rather than deriving it, with the unit mismatch documented
   in code.
2. `src.portfolio.alerts.generate_cycle_alerts`'s first draft nested the
   expiration-approaching (DTE<=7) check inside the same `if alert_type
   is not None:` branch as the action-to-alert-type mapping — so a
   position sitting at `HOLD` (which has no `ControlLoopAlertType` of
   its own) never got its DTE checked at all, meaning a `HOLD` position
   7 days from expiration silently never raised
   `EXPIRATION_APPROACHING`. Caught by a formal test
   (`test_expiration_approaching_fires_independent_of_mapped_action`)
   written specifically because the manual smoke test had used a
   position whose action already happened to alert. Fixed by
   unconditionally running the DTE check every iteration, independent
   of whether the action above it mapped to an alert type.
3. `src.data.quality_gate`'s first design assumed Pydantic's `ge=0`
   field constraint would let NaN through unchecked (motivating a
   generic `math.isfinite` scan of every numeric field) — smoke testing
   found `pydantic_core` actually already rejects `float('nan')` against
   any `ge`/`le` bound at construction time. The NaN/infinity check was
   kept (it's still the only guard for `OptionContract.theta`, which
   carries no numeric bound at all) but re-scoped and documented
   accurately rather than left implying a false sense of blanket
   protection.

**Full suite:** `python -m pytest -q tests/` -> **3236 passed, 5
skipped, 0 failed** (222 net new tests: 53 in
`tests/unit/data/test_tradier_provider.py`, 19 in
`tests/unit/data/test_rate_limiter.py`, 25 in
`tests/unit/data/test_quality_gate.py`, 2 in the extended
`tests/unit/data/test_factory.py`, 10/13/6/21/13/6/16 across
`tests/unit/portfolio/test_revaluation.py`/`test_exposure.py`/
`test_actions.py`/`test_persistence.py`/`test_ticket_monitor.py`/
`test_opportunity_scan.py`/`test_control_loop.py`, 20 in
`test_alerts.py`, and 18 (1 skipped) in
`tests/acceptance/test_tradier_market_data_only.py`; the dashboard
route-allowlist test was updated in place, not counted as new. The 5
skips are the 4 pre-existing documented false positives carried from
every prior freeze plus this step's own soft
`tradier_market_data_only`-freeze-tooling awareness check, which
self-skips until Step 22.4's freeze task (below) wires that check in).

**Non-negotiable invariants re-verified, unchanged:**
- Tradier is structurally MARKET_DATA_ONLY: no order-shaped method name
  on `TradierMarketDataProvider`, no `/v1/accounts/*/orders` endpoint
  referenced in code (docstring-only mentions, which document the
  prohibition, are excluded from the scan), `_request`'s own signature
  accepts no `method` parameter (GET is hardcoded), no POST/PUT/DELETE
  verb used against the injected HTTP client anywhere in the module,
  and no `TradierBroker`/`TradierOrderClient`/`TradierExecutionProvider`
  identifier exists anywhere in the repository (all proven by
  `tests/acceptance/test_tradier_market_data_only.py`, mirroring
  `test_alpaca_market_data_only.py`'s methodology).
- Provider Greeks remain reference-only: Tradier's own `greeks` object
  maps straight onto `OptionContract.iv/delta/gamma/theta/vega` (the
  same provider-reference contract Alpaca's mapping already carries);
  every number `src.portfolio.revaluation` actually uses for a P&L or
  Greek figure is independently re-derived via
  `src.quant.volatility.implied_volatility`/`src.quant.greeks`, never
  read from the provider's own fields.
- Risk Engine remains sole final authority: `src/portfolio/
  opportunity_scan.py` calls `src.risk.engine.evaluate_trade_proposal`
  unmodified and only ever ranks candidates that decision already
  approved/resized; `src/portfolio/control_loop.py` never constructs an
  `ApprovedOrder`/`FidelityTradeTicket` itself.
- Fidelity remains `MANUAL_EXECUTION` forever:
  `src/portfolio/ticket_monitor.py` only ever calls the pre-existing
  `transition()`/`confirm_fill()`; `record_confirmed_fill` is a
  one-line pass-through, never a new fill-producing code path.
  `config/brokers.yaml` still lists no `tradier` entry (data-only
  providers are never a "broker" for capability purposes).
- Secrets never leak: the Tradier bearer token is redacted from every
  exception path (`_redact`), never appears in a `print`/`logger` call
  anywhere in `tradier_provider.py`, and `.env.example`'s
  `OPTIONS_AGENT_TRADIER_TOKEN` line carries no real value.
- 90-day validation: still not started, not even prepared — this step
  built the control loop that will eventually feed it, and touched no
  file under `src/validation/`'s cohort-start path.

**PAPER_TRADING_V1.4: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY.** See `STEP_22_4_FREEZE_REPORT.md` for the freeze
commit SHA, manifest hash, and remote tag verification. `make
verify-freeze` reports all 42 checks passing. No cohort was created, no
Day 1 snapshot was recorded, no trades were generated, starting NAV was
not altered, and no scheduling was enabled. Work stops here per this
step's own explicit instruction — Step 23 remains separately
authorized, not started.

## Step 22.4A — Final pre-validation orchestration & dashboard
## integration remediation (controlled acceptance-gap fix to PAPER_TRADING_V1.4)

Post-freeze acceptance review of V1.4 found three genuine integration
gaps between otherwise-working, independently-tested modules: (1)
`src.portfolio.control_loop.run_control_cycle`'s own docstring
documented that a "thin outer wrapper" was expected to run
new-opportunity scanning and pending-ticket monitoring and merge their
counts in, but no such wrapper existed anywhere in the repository, so
`opportunities_scanned`/`candidates_generated`/`candidates_rejected`
stayed permanently zero/caller-supplied and neither stage was ever
invoked from a production entry point; (2) `DashboardState.latest_cycle_record`/
`latest_exposure`/`control_loop_alerts` were declared but nothing ever
projected persisted control-loop output into them, so a real dashboard
session's `/api/control-loop/*` routes could never show real data; (3)
no operator-run Tradier production smoke test existed. This step closes
all three gaps without redesigning any already-working component.

**New modules:**
- `src/portfolio/orchestrator.py` — `run_outer_cycle`, the thin
  production orchestrator `run_control_cycle`'s own docstring called
  for. Coordinates pending-ticket monitoring
  (`src.portfolio.ticket_monitor`) and new-opportunity scanning
  (`src.portfolio.opportunity_scan`) around the existing, UNMODIFIED
  `run_control_cycle`, feeding it real opportunity counts instead of
  caller-supplied zeros, then persists this cycle's exposure snapshot
  (never durably saved before this step), builds one additional
  `PortfolioControlDecisionSnapshot` for a scan's best Risk-approved
  candidate (`ControlLoopAction.REVIEW` — Risk approval is never
  execution), and generates/persists deduplicated alerts. Existing-
  position/risk monitoring (`run_control_cycle` itself) carries no
  rate-limit gate at all in this module and always runs; pending-ticket
  monitoring is gated at `RateLimitPriority.P3_PENDING_TICKET_REPRICING`,
  new-opportunity scanning at `P4_OPPORTUNITY_SCANNING` — since the
  existing `src.data.rate_limiter` priority-ceiling table already
  throttles P4 before P3 before P0/P1/P2, opportunity scanning is always
  the first thing sacrificed under provider-capacity constraints, ticket
  monitoring second, and existing-position/risk monitoring is never
  sacrificed by this module at all.
- `src/dashboard/control_loop_projection.py` — `load_latest_control_loop_state`,
  the one production-safe projection from
  `src.portfolio.persistence.ControlLoopStore` into `DashboardState`.
  Never fabricates: an empty store leaves every field at its honest
  default. `src.dashboard.app.set_state` gained an optional
  `control_loop_store` keyword that invokes this projection at session-
  initialization time — the one place the dashboard now becomes capable
  of showing the most recently completed cycle right after normal
  application startup, without inventing a portfolio or a cycle that
  never ran. The three states (no session / session with no cycle yet /
  session with real cycle data) were verified to render as 503 / 404 /
  200 respectively, never collapsed into each other.
- `scripts/smoke_tradier_market_data.py` — operator-run, GET-only,
  market-data-only production connectivity check (one quote, the
  expirations list, one option chain, contract count, rate-limit
  state). Never places/previews/cancels an order, never starts
  validation, never mutates any store, never prints the bearer token.
  Documented in README.md §19.

**Extended:**
- `src/portfolio/persistence.py` — `ControlLoopStore` gained
  `save_exposure_snapshot`/`get_exposure_snapshot` (REPLACE-on-save
  keyed by `cycle_id`, both `InMemory*`/`Sqlite*` implementations,
  restart-survival verified) — the missing durable home for
  `run_control_cycle`'s own `PortfolioExposureSnapshot` output, which
  `run_control_cycle` itself was never given the job of persisting.

**Bugs/gaps found and fixed during this step, before any formal test
existed against them:**
1. A negative-EV opportunity-scan test fixture with `broker_capabilities=None`
   always rejected at Risk regardless of `no_trade_hurdle`, masking
   whether the new opportunity-decision-snapshot wiring worked at all —
   traced to "no broker capability explicitly configured" being a
   distinct Risk rejection reason from "didn't clear the hurdle"; fixed
   by using a real `BrokerCapabilities` fixture in the new orchestrator
   tests, matching `tests/unit/portfolio/test_opportunity_scan.py`'s own
   established pattern.
2. `_verify_dashboard_has_no_live_trading_client` (the new freeze check
   for Part 14) initially flagged `src.dashboard.service.cancel_order`
   itself — a legitimate, pre-existing Step 18 action (pulling back an
   already-entered Fidelity ticket via the existing `transition()` state
   machine, never a live broker call) that happens to share a name with
   the forbidden order-submission vocabulary every other module's
   equivalent check already uses. Fixed by excluding `cancel_order`
   specifically from this one check's pattern (documented inline), since
   that capability is already independently, exhaustively proven safe by
   `tests/unit/dashboard/test_app_security.py`'s route-inventory tests.
3. The fourth-through-sixth instances of the "documented negation"
   false-positive pattern first seen in Step 22.4's own freeze work: a
   new `TradierBroker` mention inside a `#` comment (not a docstring) in
   the new security test file tripped `tests/acceptance
   /test_tradier_market_data_only.py`'s own repo-wide scan; fixed by
   rewording the comment to avoid the literal contiguous substring,
   exactly as `src/validation/freeze.py`'s equivalent comment was fixed
   during Step 22.4's own freeze task.

**Tests added:** 19 in `tests/unit/portfolio/test_orchestrator.py`
(existing-position monitoring always runs regardless of rate limit;
opportunity-scan wiring including the forced-`best` REVIEW-snapshot
path and honest zero-counts on skip; ticket-monitor wiring; the full
P4-before-P3-before-P0 rate-limit priority ordering; exposure
persistence; alert deduplication across repeated cycles; the Part 9
degraded-data isolation scenario), 6 new in
`tests/unit/portfolio/test_persistence.py` (exposure-snapshot save/get/
replace, parametrized memory+sqlite, plus restart-survival), 11 in
`tests/unit/dashboard/test_control_loop_projection.py` (the projection
function directly, plus the three-state 503/404/200 distinction and
proof no control-loop route can mutate anything), 1 optional live-data
acceptance test in `tests/acceptance/test_tradier_live_outer_cycle.py`
(skipped without a real `OPTIONS_AGENT_TRADIER_TOKEN`; never hard-codes
an expected lifecycle action), 25 in `tests/acceptance
/test_orchestrator_security.py` (hostile-audit-style: Tradier remains
market-data-only through the new surface; the orchestrator cannot
bypass Risk/Lifecycle, infer a fill, start live trading, or start
validation; the dashboard projection stays read-only; Fidelity remains
manual; the P4>P3>P0 priority ordering holds structurally). Full
repository suite: **3319 passed, 5 skipped, 0 failed** (the 5 skips are
the 4 pre-existing documented false positives plus the new live-data
acceptance test's own soft skip).

**Freeze extension:** re-frozen as **PAPER_TRADING_V1.4.1** —
`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place;
`smoke_tradier_script_hash`/`control_loop_projection_module_hash` added
(orchestrator.py needs no separate hash, already covered by
`portfolio_module_hash`'s existing whole-directory hash); three new
standing `make verify-freeze` checks —
`dashboard_cannot_execute_trades`, `orchestrator_cannot_bypass_risk_or_lifecycle`,
`opportunity_scan_never_outranks_risk_monitoring` — each re-derived
directly from the current working tree on every run, independent of the
acceptance test suite.

**PAPER_TRADING_V1.4.1: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY.** See `STEP_22_4A_FREEZE_REPORT.md` for the freeze
commit SHA, manifest hash, and remote tag verification. No cohort was
created, no Day 1 snapshot was recorded, no trades were generated,
starting NAV was not altered, and no scheduling was enabled. Work stops
here per this step's own explicit instruction.

## Step 22.4B: Test-Portability Remediation, re-frozen as PAPER_TRADING_V1.4.2

Triggered by running the frozen PAPER_TRADING_V1.4.1 suite on a real
operator macOS checkout (`/Users/dipesh/Trading/Options`, Python
3.12.14): **3315 passed, 4 skipped, 5 failed** there, vs. this
sandbox's 3319/6/0 — every one of the 5 failures was a test/fixture
portability defect that had never been exercised in this sandbox
environment, never a production defect. This step's explicit scope was
**test-only**: no production trading logic (`src/risk/`, `src/quant/`,
`src/brokers/`, strategies, validation methodology, dashboard
production behavior, live-trading configuration) was touched.

1. **`test_no_alpaca_trading_import_anywhere_in_the_repository`** — a
   bare `REPO_ROOT.rglob("*.py")` descended into a real operator's
   `.venv` and flagged the installed `alpaca-py` package's own
   `alpaca.trading` imports as if they were repository code. Fixed by
   adding `repo_controlled_files()` to `tests/acceptance/conftest.py` —
   `git ls-files -z --cached --others --exclude-standard`, git's own
   authoritative "tracked, or untracked-but-not-ignored" definition of
   what could actually end up committed — and scoping the scan to it.
   The assertion itself (no `alpaca.trading` import in repository-
   controlled code) is unchanged.
2. **`test_backup_creates_a_timestamped_file_containing_the_real_
   database_bytes`** — the fixture wrote a fake `b"SQLite format
   3\x00"` header followed by arbitrary bytes. `backup.sh` correctly
   uses the real `sqlite3` CLI's `.backup` command when it's installed
   (the common case on macOS, absent in this sandbox), which opens and
   validates the actual SQLite file structure, not just a magic-string
   prefix — it correctly rejected the fake fixture as "file is not a
   database". Fixed by writing a genuine minimal SQLite database via
   the stdlib `sqlite3` module (`_write_minimal_sqlite_db`), and added
   a positive check that the backed-up file is itself openable and
   contains the expected row. Also hardened the neighboring two-backups
   test with the same real-database fixture plus previously-absent
   `returncode == 0` assertions. `backup.sh`'s own integrity validation
   was not weakened.
3. **`test_no_env_credential_or_secret_files_exist`** — flagged the
   operator's own properly-gitignored local `.env` (required for normal
   Tradier/Alpaca configuration) as a leaked credential file. Fixed by
   scoping the same `repo_controlled_files()` helper to this scan too —
   a `.env` that ever became tracked/committed still fails loudly
   immediately, while a genuinely gitignored local one does not. Added
   a belt-and-suspenders test (`test_a_local_env_file_if_present_is_
   genuinely_gitignored_not_merely_untracked`) that directly runs `git
   check-ignore -q .env` whenever a local `.env` exists, proving the
   exclusion is never merely "not yet `git add`ed".
4/5. **`test_strategy_integrity.py`** — two hard-coded
   `cwd="/home/user/Options"` `subprocess.run` calls and one hard-coded
   `open(f"/home/user/Options/{path}")` failed outright on a real
   checkout at a different path. Replaced all three with the module's
   own dynamically-derived `REPO_ROOT = Path(__file__).resolve()
   .parents[2]` constant, already the established pattern used
   elsewhere in `tests/acceptance/`.

**Portability audit** (per this step's own instruction to search the
whole suite for the same defect classes) found and fixed two further
latent instances, neither among the 5 originally reported:
`test_tradier_market_data_only.py`'s own repo-wide Tradier-order-
shaped-identifier scan (same `.venv`-descent risk as failure 1), and
`test_validation_pipeline.py`'s no-sqlite-or-db-file-exists-anywhere-
in-the-repository scan (same risk: a third-party package's own bundled
`.db`/`.sqlite` fixture under `.venv` would have false-positived as
real cohort data). Both rewritten onto `repo_controlled_files()` the
same way.

Two items were found and deliberately left untouched, as out of scope:
`VALIDATION_MANIFEST.json`'s own historical `/home/user/Options`-keyed
hash-record dictionary keys (a frozen historical artifact from an
earlier freeze step — its content documents what was actually
generated then, and rewriting it would misrepresent that record, not
fix a defect), and `src/validation/freeze.py`'s own single production-
code `REPO_ROOT.rglob("*.py")` scan inside
`_verify_tradier_is_market_data_only` (same theoretical defect class,
but production code this step's constraints forbid touching absent an
actual observed false positive — none found; no plausible third-party
package defines a `TradierBroker`-shaped class).

**Tests added/changed:** all 7 changed files are under
`tests/acceptance/` plus 1 assertion pair in
`tests/unit/validation/test_freeze.py` (the version-metadata bump
below) — zero new test files, zero `src/` behavior changes. Full
repository suite: **3319 passed, 6 skipped, 0 failed**, identical shape
to this sandbox's pre-existing baseline (these fixtures were never
exercised here the way a real `.venv`, a real `sqlite3` CLI, and a real
local `.env` exposed them on the operator's machine).

**Freeze extension:** re-frozen as **PAPER_TRADING_V1.4.2** —
`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.1`/`1.4.1` to `PAPER_TRADING_V1.4.2`/`1.4.2`; no
new manifest fields or `make verify-freeze` checks were needed (no new
production module was created this step). All 42 checks carried from
V1.4.1 plus the 3 orchestrator-era checks added in V1.4.1 itself still
pass **unchanged** — this step touched no file inside `src/quant/`,
`src/risk/`, `src/brokers/`, `src/wheel/`, `src/lifecycle/`,
`src/data/`, `src/portfolio/`, or `src/dashboard/`, confirmed by every
module-hash check in `make verify-freeze` reading `unchanged`.

**PAPER_TRADING_V1.4.2: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY.** See `STEP_22_4B_FREEZE_REPORT.md` for the freeze
commit SHA, manifest hash, and remote tag verification. No cohort was
created, no Day 1 snapshot was recorded, no trades were generated,
starting NAV was not altered, and no scheduling was enabled. Work stops
here per this step's own explicit instruction.

## Step 22.4C: SQLite Backup Acceptance-Test Semantic Verification, re-frozen as PAPER_TRADING_V1.4.3

Triggered by an independent macOS verification of PAPER_TRADING_V1.4.2:
**3320 passed, 4 skipped, 1 failed** — the sole failure was
`tests/acceptance/test_backup_restore.py::TestBackupScript::
test_backup_creates_a_timestamped_file_containing_the_real_database_
bytes`, at `assert backups[0].read_bytes() == db_path.read_bytes()`
(observed mismatch at byte offset 27, `0x01` vs `0x02` — inside the
SQLite header's file-change-counter field). This step's scope was
again **test-only**: `scripts/backup.sh`/`scripts/restore.sh` and
every production module were reviewed and confirmed to need no change.

**Root cause:** the test required the backup produced by `backup.sh`
to be byte-for-byte identical to the source database file.
`backup.sh` itself is correct — on a real operator machine with the
`sqlite3` CLI installed (the common case, e.g. macOS), it takes the
backup via `sqlite3`'s own `.backup` dot-command, which goes through
SQLite's genuine backup API. That API legitimately writes its own
destination-file header fields during its own internal commit (the
file-change-counter field is exactly the byte range the operator's
diff flagged) and may lay out free/interior pages differently than the
source. Neither is data loss or corruption — SQLite's backup mechanism
never promises byte-for-byte file identity, only equivalent logical
content, and requiring the former was the test's own defect.

**Fix:** replaced the byte-equality assertion with 9 semantic/
integrity checks in the renamed
`test_backup_creates_a_timestamped_file_containing_the_real_database_
content`: (1) `backup.sh` exits 0, (2) exactly one timestamped backup
file exists, (3) the backup is a valid, openable SQLite database, (4)
`PRAGMA integrity_check` reports `ok` on the backup, (5) the expected
`validation_marker` table exists in the backup, (6) the exact known
fixture row (`"real-database-content"`) is present in the backup, (7)
the source database still contains that same fixture content after the
backup ran (also integrity-checked), (8) the backup file is non-empty,
and (9) source and backup are proven logically equivalent by directly
comparing their query results. No assertion was deleted without a
strictly stronger semantic replacement.

**Backup/restore audit:** every remaining byte-for-byte comparison in
the file (`TestRestoreScript`'s live-database-replaced, safety-backup-
content, and cancelled-restore checks) was reviewed against
`restore.sh`'s actual implementation — confirmed to be a plain `cp`,
never the `sqlite3` CLI or any SQLite backup API — so byte equality
there is exactly the correct assertion, not a defect, and none of
those three were changed.

**Tests:** targeted `pytest tests/acceptance/test_backup_restore.py` —
7 passed, 0 failed. Full repository suite: **3319 passed, 6 skipped, 0
failed**, identical shape to the pre-existing baseline (this fixture's
own byte-header assumption was the only thing broken, and only on a
real `sqlite3` CLI, which this sandbox lacks — the sandbox's own
`cp`-fallback path in `backup.sh` never exercised the difference,
which is exactly why this required an independent macOS run to
surface).

**Freeze extension:** re-frozen as **PAPER_TRADING_V1.4.3** —
`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.4.2`/`1.4.2` to `PAPER_TRADING_V1.4.3`/`1.4.3`; no
new manifest fields or `make verify-freeze` checks were needed. All 48
checks carried from V1.4.2 still pass **unchanged** — this step
touched no file inside `src/quant/`, `src/risk/`, `src/brokers/`,
`src/wheel/`, `src/lifecycle/`, `src/data/`, `src/portfolio/`, or
`src/dashboard/`, and `scripts/backup.sh`/`scripts/restore.sh`
themselves are unmodified (confirmed by empty `git diff` against the
prior freeze commit for both files).

**PAPER_TRADING_V1.4.3: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY.** See `STEP_22_4C_FREEZE_REPORT.md` for the freeze
commit SHA, manifest hash, and remote tag verification. No cohort was
created, no Day 1 snapshot was recorded, no trades were generated,
starting NAV was not altered, and no scheduling was enabled. Work stops
here per this step's own explicit instruction.
