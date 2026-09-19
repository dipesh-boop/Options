# Progress

Living status tracker for the systematic options research & paper-trading
platform. Update this file at the end of every work session — append,
don't rewrite history.

## Status: LLM orchestration layer (Phase 5 plumbing) implemented ahead of
## order; Phase 0 foundations (DB, config, CI) still not started

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

## Open decisions carried forward (updated)

- [ ] Historical options data vendor for backtesting (Phase 2 blocker)
- [ ] Schwab paper-trading / sandbox capability (Phase 4 blocker)
- [ ] Sector/classification data source for correlation/concentration checks
- [ ] Final ~50-name equity universe list + liquidity criteria
- [ ] **`app/` prototype disposition — now fully redundant, recommend
      resolving before Phase 0 (see IMPLEMENTATION_PLAN.md §7)**
- [ ] Fold `src/llm/` + `src/quant/` under `src/options_platform/`, or
      keep `src/` flat with multiple top-level packages
- [ ] Model tier per non-Portfolio-Manager agent role
- [ ] MAX_MARKET_DATA_AGE (15 min, in `src/llm/schemas.py`) is a
      placeholder pending real config, same as risk-per-trade-pct and
      other numeric policy constants used in quant tests/examples

## Next up

- `src/llm/` (agent plumbing) and `src/quant/` (deterministic
  calculations) both exist and are each internally tested, but nothing
  connects them yet — no DB, no Strategy Screener, no Python Risk Engine
  (portfolio-state-aware limits, RiskGate, circuit breaker), no
  broker/backtest data feeding real numbers into either layer. Awaiting
  direction: keep building deterministic/LLM-adjacent pieces in isolation
  (e.g. Python Risk Engine next, since Quant now has numbers for it to
  gate), or start Phase 0 foundations (repo scaffolding, DB schema, CI)
  so there's a real pipeline to wire both into. Also still open: the
  `app/` prototype disposition (now fully redundant — see above) and the
  `src/llm/` + `src/quant/` vs `src/options_platform/` layout
  reconciliation.
