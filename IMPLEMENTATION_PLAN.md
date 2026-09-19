# Implementation Plan — Systematic Options Research & Paper-Trading Platform

Companion to `ARCHITECTURE.md`. This document covers folder structure,
dependencies, and phased delivery. **The LLM orchestration layer
(`src/llm/`, `.claude/agents/`, `config/llm.yaml`) has been implemented,
ahead of the phase order below, at explicit request — see §6 and
`progress.md` for what exists and what doesn't yet.**

## 1. Proposed folder structure

```
Options/
├── ARCHITECTURE.md
├── IMPLEMENTATION_PLAN.md
├── progress.md
├── README.md
├── pyproject.toml
├── requirements/
│   ├── base.txt
│   └── dev.txt
├── .env.example
├── alembic/
│   ├── env.py
│   └── versions/
├── src/
│   └── options_platform/
│       ├── __init__.py
│       ├── config.py               # pydantic-settings, per-environment
│       ├── core/
│       │   ├── enums.py            # TradingMode, StrategyType, Right, OrderStatus, RiskFlag
│       │   ├── models.py           # shared Pydantic domain models (contract, quote, etc.)
│       │   └── exceptions.py
│       ├── db/
│       │   ├── base.py             # SQLAlchemy engine/session
│       │   ├── models/             # ORM: instruments, quotes, positions, orders, fills,
│       │   │                       #      accounts, risk_limits, agent_decisions, backtest_runs
│       │   └── repositories/       # one repository per aggregate; only layer touching SQL
│       ├── market_data/
│       │   ├── provider.py         # abstract MarketDataProvider
│       │   ├── ibkr_provider.py
│       │   ├── schwab_provider.py
│       │   ├── mock_provider.py
│       │   └── historical/         # historical data adapter(s) for backtesting
│       ├── brokers/
│       │   ├── base.py             # abstract BrokerClient
│       │   ├── ibkr_broker.py
│       │   ├── schwab_broker.py
│       │   ├── paper_broker.py     # simulated fills for brokers without a paper sandbox
│       │   └── backtest_broker.py  # no network code path, by construction
│       ├── quant/                  # per-trade calculation, no policy — pure functions
│       │   ├── greeks.py
│       │   ├── pnl.py
│       │   ├── max_loss.py
│       │   └── expected_value.py
│       ├── risk/                   # portfolio policy & gating — the trusted kernel
│       │   ├── position_sizing.py
│       │   ├── exposure.py
│       │   ├── correlation.py
│       │   ├── drawdown.py
│       │   ├── limits.py           # RiskGate
│       │   └── circuit_breaker.py
│       ├── strategies/
│       │   ├── base.py
│       │   ├── cash_secured_put.py
│       │   ├── covered_call.py
│       │   ├── put_credit_spread.py
│       │   └── screener.py         # universe + liquidity + criteria filtering
│       ├── agent/                  # Multi-Agent Layer — see ARCHITECTURE.md §5
│       │   ├── client.py           # Anthropic API wrapper
│       │   ├── schemas.py          # strict typed I/O contracts (no numeric-risk fields), incl. TradeProposal
│       │   ├── tools.py            # read-only tool definitions only, shared by all four roles
│       │   ├── portfolio_manager.py    # orchestrates the three roles below; Opus-tier recommended
│       │   ├── market_agent.py         # read-only research/context
│       │   ├── strategy_analyst.py     # proposes structures within the eligible candidate set
│       │   ├── adversarial_reviewer.py # red-teams each proposal; advisory only
│       │   ├── prompts/
│       │   └── audit.py            # persist every agent call, all four roles, append-only
│       ├── backtest/
│       │   ├── engine.py
│       │   ├── fills.py            # slippage/commission model
│       │   └── metrics.py          # CAGR, Sharpe, Sortino, max DD, win rate
│       ├── orchestration/
│       │   ├── pipeline.py         # data -> screen -> agent layer -> quant -> risk-gate -> mode-gated execute -> log
│       │   ├── scheduler.py
│       │   └── modes.py            # TradingMode enum + hard LIVE-not-implemented guard
│       ├── api/
│       │   ├── main.py             # FastAPI app
│       │   ├── routes/
│       │   └── schemas/            # API request/response models
│       └── web/                    # dashboard (static/templates or a small SPA)
├── tests/
│   ├── unit/                       # risk math, screeners, schemas — no I/O
│   ├── integration/                # DB + mock provider + mock broker
│   └── backtest/                   # backtest engine correctness (no lookahead, known fixtures)
└── scripts/                        # db init/seed, universe list generation, ops one-offs
```

Rationale for a `src/` layout with a single installable package
(`options_platform`): keeps the broker/LLM/DB adapters cleanly separated
from the domain core (hexagonal-style — `quant/`, `risk/`, and
`strategies/` depend on nothing external and are the easiest to test
exhaustively), and avoids accidental imports of test code or scripts into
the package. `quant/` (per-trade math, no portfolio state) and `risk/`
(portfolio policy/gating, the RiskGate) are deliberately separate packages
— see `ARCHITECTURE.md` §6–§7 — so "what are the numbers" and "is this
allowed" stay independently testable and neither can silently absorb the
other's responsibility.

## 2. Proposed dependencies

Core:
- `python` 3.12+
- `fastapi`, `uvicorn[standard]`
- `pydantic` v2, `pydantic-settings`
- `sqlalchemy` 2.x, `alembic`, `psycopg[binary]`
- `pandas`, `numpy`, `scipy`
- `anthropic` (Claude API SDK)
- `ib_insync` (IBKR)
- Schwab client: community `schwab-py`, or direct `httpx` + OAuth2 —
  decided in Phase 4 once Schwab's current API/sandbox is researched
- `httpx` (general REST)
- `pandas_market_calendars` (exchange calendar / holiday handling)
- `tenacity` (retry/backoff with jitter)
- `structlog` (structured logging) or stdlib `logging` with a JSON formatter
- `apscheduler` (or a small asyncio-based scheduler) for orchestration cadence
- `python-dotenv`

Dev/test:
- `pytest`, `pytest-asyncio`, `pytest-cov`
- `hypothesis` (property-based tests for the risk math — e.g. spread max
  loss must never exceed width−credit under any generated inputs)
- `ruff` (lint+format), `mypy` (type checking — important given the
  numeric/financial surface area)
- `pip-audit` (dependency vulnerability scanning in CI)

Deferred / TBD (Phase 2+, pending the open questions in
`ARCHITECTURE.md` §10):
- Historical options data vendor client (CBOE DataShop / ORATS /
  Polygon.io / ivolatility — not chosen yet)
- Sector/classification data source for correlation/concentration checks

## 3. Disposition of the existing prototype

Before this message, a small standalone prototype (Greeks/Black-Scholes
solver, a synthetic mock data provider, Pydantic option-chain models) was
started directly under `app/` at the repo root, based on an earlier,
much-narrower "options analysis dashboard" scope. It is **not part of
this architecture** (wrong package layout, no DB, no risk gate, no
strategy/broker/agent separation) but the Black-Scholes/IV-solver math in
`app/analytics/greeks.py` is directly reusable as the seed for
`src/options_platform/quant/greeks.py` in Phase 0/1 rather than being
rewritten from scratch. Recommend deciding in the next session whether to
port-then-delete `app/`, or delete it outright and rewrite — flagged here
rather than acted on, since this phase is design-only.

## 4. Phased delivery plan

Each phase ends with passing tests and a `progress.md` update before the
next begins. `LIVE` trading is out of scope for every phase below; it is
not scheduled.

### Phase 0 — Foundations
- Repo scaffolding per §1, `pyproject.toml`, lint/type-check/test CI.
- `config.py` (pydantic-settings), structured logging setup.
- Postgres schema + Alembic migrations for: instruments, quote snapshots,
  positions, orders, fills, accounts, `risk_limits` (versioned), 
  `agent_decisions` (append-only), `backtest_runs`.
- Core domain enums/models (`TradingMode` with only BACKTEST/SHADOW/PAPER).
- Decision + action on the existing `app/` prototype (§3).

### Phase 1 — Deterministic core (no broker, no LLM)
- **Python Quant implemented ahead of phase order** (see §6): Black-Scholes
  pricing (`src/quant/black_scholes.py`), independent closed-form Greeks
  plus a position-level `net_greeks` aggregator (`greeks.py`), an implied
  volatility solver (`volatility.py`), risk-neutral probability ITM /
  probability of profit (`probability.py`), per-strategy max
  profit/loss/breakeven/ROC/annualized ROC/expected value for all three
  initial strategies (`expected_value.py`), Monte Carlo terminal-price
  simulation plus a deterministic stress grid at the required
  -20/-10/-5/+5/+10/+20% spot shocks and a vol-shock grid
  (`monte_carlo.py`), fixed-fractional position sizing
  (`position_sizing.py`), and return-correlation analysis
  (`correlations.py`). 197 tests, several using finite-difference and
  put-call-parity cross-checks against the pricing function itself
  rather than hardcoded reference numbers.
- **Market Data Layer also implemented ahead of phase order** (see §6):
  canonical schemas every provider must convert into (`src/data/`) —
  `OptionContract` (delta/gamma/theta/vega/IV as provider-reported
  reference values, not independently computed — src/data has no
  dependency on src/quant, same one-way-boundary pattern as src/llm),
  `OptionChain`, `UnderlyingQuote`, `HistoricalBar`, `EarningsEvent` —
  plus the abstract `MarketDataProvider` / `HistoricalDataProvider` /
  `EarningsCalendarProvider` interfaces, freshness enforcement
  (`assert_tradable` raises `StaleDataError` on stale data — "prohibit
  trade approval" is an exception, not a status flag), a no-lookahead
  guard for backtesting (`assert_no_lookahead`), and an
  `ensure_canonical` boundary guard so a raw provider response can never
  reach the LLM layer. 94 tests. **No concrete provider exists yet** —
  no mock, no IBKR, no Schwab — only the canonical shapes and the
  abstract contracts a concrete adapter must satisfy.
- **Not yet implemented:** Python Risk Engine (position sizing exists as
  a pure calculation, but portfolio-state-aware exposure, correlation
  limits, drawdown monitoring, the RiskGate, and the circuit breaker do
  not), Strategy Screener, and any concrete market data provider (mock or
  real) — every quant function takes plain numeric parameters (spot,
  strike, sigma, …) and every data-layer type is constructed directly in
  tests, not pulled from a screener, DB, or live connection, so nothing
  in Phase 1 is blocked on those yet, but nothing is wired end-to-end
  either.
- Heavy unit + property-based test coverage — this phase is the trust
  foundation for everything after it.

### Phase 2 — Backtesting engine
- Resolve the historical-options-data open question (§10 in
  `ARCHITECTURE.md`) before building the ingestion adapter.
- `BacktestBroker`, fill/slippage/commission model, point-in-time data
  discipline (no lookahead).
- Performance metrics (CAGR, Sharpe, Sortino, max drawdown, win rate) and
  a benchmark comparison (e.g. vs. SPY buy-hold).
- First backtests of CSP/covered-call/put-credit-spread over the initial
  universe.

### Phase 3 — Broker abstraction + IBKR (paper only)
- `BrokerClient` interface; `IBKRBroker` via `ib_insync` against a paper
  account (distinct port from live, never the same credential path).
- Order lifecycle, reconciliation on reconnect/startup, idempotent order
  keys.
- `SHADOW` mode end-to-end against live market data (decisions logged,
  nothing submitted).

### Phase 4 — Schwab integration
- Research current Schwab developer API/sandbox capability first (this is
  an open question, not assumed); decide data-only vs. paper-order-capable
  based on findings.
- `SchwabBroker` implementation to whatever extent the sandbox allows;
  `PaperBroker` simulated-fill fallback for anything it doesn't.

### Phase 5 — Multi-Agent Layer
- **LLM plumbing implemented ahead of phase order** (see §6): Anthropic
  API client wrapper with forced tool-use structured output
  (`src/llm/client.py`), strict Pydantic I/O schemas where
  `TradeProposal` is the only order-adjacent type (`src/llm/schemas.py`),
  a configurable task_type → tier → model router reading
  `config/llm.yaml` (`src/llm/router.py`), read-only context builders
  (`src/llm/context.py`), and `.claude/agents/*.md` persona definitions +
  prompt assembly (`src/llm/prompts.py`) for all eight roles: Portfolio
  Manager, Market Regime, Opportunity Scanner, Strategy Analyst, Devil's
  Advocate, Risk Reviewer, Trade Manager, Performance Auditor.
- **Not yet implemented:** the audit-log persistence table (needs
  Phase 0's DB schema), the read-only tool *implementations* the agent
  definitions reference (they need real screener/quant/risk-engine/
  position data to read from, none of which exists yet), and wiring any
  of this into the orchestration pipeline.
- When Phase 0–1 exist, wire the Multi-Agent Layer into the pipeline
  strictly downstream of the Strategy Screener and strictly upstream of
  Python Quant and Python Risk Engine, which reprice and gate every
  `TradeProposal` before it can become an order in any mode
  (`ARCHITECTURE.md` §9).

### Phase 6 — PAPER trading end-to-end
- Scheduler + orchestrator running the full cycle in `PAPER` mode.
- Dashboard: portfolio risk view, screened candidates, agent rationale/
  audit trail, backtest results viewer.
- Live-fire testing of the circuit breaker and kill switch under
  simulated failure conditions.

### Phase 7 — Hardening
- Security review against `ARCHITECTURE.md` §8.
- Deliberate failure-mode testing from §9 (forced disconnects, partial
  fills, stale data, clock edge cases, corporate actions).
- Documentation pass. Any future consideration of `LIVE` trading is an
  explicit, separate, future decision requiring human sign-off — not
  scheduled as part of this plan.

## 5. Non-goals for this document / this phase

Per the instruction that produced this plan: no trading system code is
being written in this phase. This plan intentionally does not: pick the
final ~50-name universe, choose the historical data vendor, or write any
`src/options_platform/` code. Those are first work items *inside* Phases
0–2, not decided here.

## 6. Deviation from §1: where the LLM layer actually landed

The LLM orchestration layer was implemented at explicit, later request
using the exact paths specified at that time: `src/llm/` (not nested
under `src/options_platform/`), `config/llm.yaml` (top-level `config/`,
not under `src/options_platform/`), and `.claude/agents/*.md` for the
eight agent personas. This is a real deviation from the `src/` layout in
§1, not a typo — recorded here rather than silently reconciled. It works
standalone today (its tests mock the Anthropic API and never touch a
DB), but Phase 0 needs to decide: fold `src/llm/` under
`src/options_platform/` for a single installable package, or keep `src/`
flat with multiple top-level packages (`llm/`, and later `quant/`,
`risk/`, etc.) as siblings. Not decided here — flagged for that phase.

Also implemented ahead of order: no trading-system code exists yet (no
DB, no broker, no quant/risk engine, no screener), consistent with "do
not implement broker execution yet." The LLM layer's tests prove its own
internal contract (malformed/execution-shaped output can't validate, and
even a validated non-`TradeProposal` object can't pass the
`ensure_trade_proposal` boundary guard) but cannot yet prove anything
about a live pipeline, because there isn't one to plug into.

## 7. Deviation from §1: where the Quant layer actually landed

Same pattern as §6: the deterministic quant engine was implemented at
`src/quant/` directly (not `src/options_platform/quant/`), continuing
rather than resolving the layout question — `src/` now has two top-level
packages (`llm/`, `quant/`) instead of one `options_platform/` package.
Still not decided here; still flagged for Phase 0.

Unlike the LLM layer, `src/quant/` has a verified one-way dependency
boundary: `tests/unit/quant/test_architecture_boundary.py` scans every
module in the package for an `import`/`from` statement referencing
`src.llm` and fails if one is found. Python Quant can be used completely
independently of the Multi-Agent Layer (every function takes plain
numeric parameters — spot, strike, sigma, legs — never a
`TradeProposal`), which is what makes "the LLM must consume these
calculations, never replace them" enforceable rather than aspirational:
there is no import cycle available even if someone tried to create one
the wrong way round.

**This makes the pre-existing `app/` prototype's `app/analytics/greeks.py`
fully redundant**, not just partially as before (`src/quant/black_scholes.py`
+ `greeks.py` + `volatility.py` now cover the same ground with
independent verification the prototype never had — finite-difference
Greek cross-checks, put-call parity, Monte Carlo convergence). The `app/`
disposition question from §3/progress.md is now higher-stakes to leave
open; recommend resolving it before Phase 0 rather than during it.

## 8. Deviation from §1: where the Market Data Layer actually landed

Same pattern again: `src/data/` (not `src/options_platform/data/` or
`src/options_platform/market_data/`). `src/` now has three top-level
packages (`llm/`, `quant/`, `data/`). Still not decided here.

`src/data` holds the same verified one-way boundary as `src/quant`
(`tests/unit/data/test_architecture_boundary.py`), and additionally has
**no dependency on `src/quant` either** — a deliberate choice, not an
oversight. `OptionContract.iv/delta/gamma/theta/vega` are provider-
reported reference values (what a broker's own model said), not
Python Quant's independently computed ones; keeping the layers
decoupled keeps that distinction structurally visible rather than
implicit. One consequence worth naming: `src/data`, `src/quant`, and
`src/llm` each now define their own tiny `OptionRight`/`Right` enum —
three copies of the same two-value type. A shared `src/core/`
primitives module is the obvious Phase 0 cleanup for this and the
`src/llm` vs `src/options_platform` layout question together, rather
than fixing one without the other.

This is also the third time in a row that a request for one piece of
the platform has been implemented standalone, ahead of Phase 0/1 order,
each with its own placeholder policy constant
(`src.llm.schemas.MAX_MARKET_DATA_AGE`, now also
`src.data.provider.DEFAULT_MAX_QUOTE_AGE` — same 15-minute value,
same "TODO(Phase 0): move to config" note, declared independently
twice). Worth deciding soon whether to keep building sideways like this
or consolidate into Phase 0 foundations before a fourth constant shows
up — flagged, not decided here.
