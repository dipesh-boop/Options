# Implementation Plan — Systematic Options Research & Paper-Trading Platform

Companion to `ARCHITECTURE.md`. This document covers folder structure,
dependencies, and phased delivery. **No implementation has started under
this plan yet** — see `progress.md` for live status.

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
- Python Quant: Greeks, P&L, max loss, expected value — pure per-trade
  calculation, no portfolio state.
- Python Risk Engine: position sizing, exposure, correlation, drawdown
  monitor, RiskGate, circuit breaker — portfolio policy, built on top of
  Quant's numbers.
- Strategy screeners: CSP, covered call, put credit spread, universe +
  liquidity filtering (min OI/volume, max spread%, DTE window, no
  earnings-week entries).
- Mock market data provider (synthetic but internally consistent chains)
  so everything above is testable without any external dependency.
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
- Anthropic API client wrapper, strict Pydantic I/O schemas (no numeric
  risk fields, incl. the `TradeProposal` intent object — see
  `ARCHITECTURE.md` §2, §5), read-only tool definitions only (shared
  across all four roles), append-only audit logging.
- Implement the four roles: Market Agent, Strategy Analyst, Adversarial
  Reviewer, and the orchestrating Portfolio Manager (Opus-tier
  recommended; sub-agent tier configurable — open question 5 in
  `ARCHITECTURE.md` §12).
- Wire into the orchestration pipeline strictly downstream of the
  Strategy Screener and strictly upstream of Python Quant and Python Risk
  Engine, which reprice and gate every proposal before it can become an
  order in any mode (`ARCHITECTURE.md` §9).

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
