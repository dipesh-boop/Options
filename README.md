# Options Trading Research Platform

A research and paper-trading assistant for stock options strategies. It
watches the market, proposes and prices specific option trades, checks
every trade against strict risk rules, and tracks how those trades
would have performed — all on paper, in simulation. It never places a
real order by itself.

This document assumes no programming background. If a term is
unfamiliar (e.g. "iron condor," "paper trading"), it is explained the
first time it appears.

---

## 1. What this application does

- Scans option chains for candidate trades using a fixed library of 16
  named strategies (covered calls, credit spreads, iron condors, and
  so on).
- Prices each candidate with deterministic math (Black-Scholes option
  pricing, Monte Carlo simulation) — never guesses a price.
- Runs every candidate through a Risk Engine that can approve it,
  shrink its size, reject it, or halt trading entirely. The Risk
  Engine's decision is final; nothing in this system can override it.
- Optionally asks an AI model (Claude) to propose trade ideas, play
  devil's advocate against them, and act as a skeptical CIO — but the
  AI's opinion never bypasses the Risk Engine, and the AI never
  computes its own prices or risk figures.
- Simulates trades in a built-in "paper broker" so you can see
  realistic (not perfect, frictionless) results before ever risking
  real money.
- Backtests strategies against historical option data.
- Produces a **Fidelity ticket** — a plain-text summary of exactly
  what to type into Fidelity's own order-entry screen — for trades you
  decide to actually place. You still do that typing yourself.
- Tracks a 90-day paper-trading validation protocol so you can judge,
  with real statistics, whether the system's picks are actually good
  before trusting it with real capital.

## 2. What this application does NOT do

- It does **not** connect to Fidelity, Schwab, IBKR-live, or any real
  brokerage automatically. There is no code anywhere in this
  repository that can submit a real securities order.
- It does **not** store your brokerage username, password, MFA code,
  or session cookies. There is nothing to log into.
- It does **not** scrape or automate a web browser against Fidelity's
  website or Trader+ platform.
- It does **not** guarantee profit. The platform's own research target
  (12-15% annualized) is explicitly described everywhere in the code
  as aspirational, not a promise — and the system is built to hold
  cash or reject a trade rather than force one to happen.
- It does **not** trade live money under any configuration in this
  repository. Every execution path is either an internal simulation
  ("paper trading") or a human manually typing an order into Fidelity
  themselves.

## 3. How the architecture works (in plain English)

Think of it as an assembly line with a strict inspector at every
station:

1. **Market data** comes in (stock/option prices).
2. **Quant engine** (pure math, no AI) prices every possible trade
   structure — profit, loss, probability, and more.
3. **Strategy layer** compares every strategy the platform knows
   against the current market and ranks the best few, including the
   option of doing nothing ("CASH / NO_TRADE").
4. **AI agents** (optional) can propose ideas and critique them in
   plain language, but every number they mention has to come from the
   quant engine above — the AI is never allowed to invent a price or a
   risk figure.
5. **Risk Engine** (pure math, no AI) is the only thing that can
   approve, resize, reject, or halt a trade. It checks position size,
   portfolio concentration, cash reserves, stress scenarios, and more.
   It can only ever make a trade *smaller* than requested, never
   larger.
6. **Execution**: either the built-in paper broker fills the trade in
   simulation, or — for real accounts — a **Fidelity ticket** is
   generated for a human to manually enter in Fidelity Trader+. Real
   money never moves without that manual step.
7. **Dashboard**: a web page (running on your own computer) where you
   review today's opportunities, see the risk state, copy a Fidelity
   order's text, and mark trades as entered/rejected.
8. **Validation & reporting**: every simulated trade is logged, so
   daily/weekly/monthly reports and a formal 90-day validation
   scorecard can tell you honestly how the system is doing.

For the full technical breakdown, see `ARCHITECTURE.md`.

## 4. How to install it

You need Python 3.11 or newer.

```bash
git clone <this repository>
cd Options
python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

(Or, if you have `make`: `make install`.)

## 5. How to configure it

1. Copy the example environment file and fill in your own values:

   ```bash
   cp .env.example .env
   ```

2. Open `.env` in a text editor. At minimum, set:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   This is only needed if you want the AI agents (Portfolio Manager,
   Devil's Advocate, Strategy Research) to run. The quant engine, risk
   engine, paper broker, and dashboard all work without it.

3. Never commit `.env` to git. It is already excluded via
   `.gitignore`. `.env.example` (which has no real secrets) is the
   only one meant to be shared or committed.

4. Broker/account settings (which strategies each account is approved
   to trade, execution mode) live in `config/brokers.yaml`. Risk
   limits live in `config/risk_limits.yaml`. Both are plain YAML files
   with comments explaining every setting — you do not need to touch
   either to get started; the shipped defaults are safe, conservative
   starting points.

## 6. How to start it

```bash
./scripts/start.sh
```

(Or: `make run`.)

This starts the dashboard's web server. You should see:

```
Starting dashboard at http://127.0.0.1:8000 ...
...
Uvicorn running on http://127.0.0.1:8000
```

## 7. How to open the dashboard

Once the server is running, open a web browser and go to:

```
http://127.0.0.1:8000
```

You'll see today's candidate opportunities (if any have been
generated), the current risk state, and buttons to refresh a price,
view the full analysis on a trade, copy a Fidelity order's text, mark
an order as entered, or reject a trade. There is deliberately **no**
"Auto Trade," "Execute," or "Send to Fidelity" button anywhere — those
do not exist in this system.

## 8. How paper trading works

"Paper trading" means simulated trading: the platform pretends to
place an order and tracks what would have happened, using realistic
(not perfect) fill prices, commissions, and slippage — but no real
money or real brokerage account is ever involved. This is how the
system is validated before anyone would trust it with real capital.
Every paper fill is clearly separate from a real Fidelity fill; the
two can never be confused with each other anywhere in the code.

## 9. How Fidelity manual execution works

For a trade you decide to actually place with real money, the
dashboard generates a **Fidelity ticket**: a plain-text summary
showing every leg of the trade (buy/sell, strike, expiration,
put/call, quantity), the target price, minimum acceptable price,
current max profit/loss, breakeven(s), and more. You copy that text,
open Fidelity Trader+ yourself, and manually type in the order exactly
as shown. After you actually enter it in Fidelity, you mark the ticket
as "order entered" in the dashboard, and once Fidelity confirms your
fill, you record that confirmation too. The system tracks this whole
lifecycle (awaiting you → you entered it → confirmed filled) but never
performs any of those steps for you.

## 10. How to stop it

Press `Ctrl-C` in the terminal where `./scripts/start.sh` is running.

## 11. How to restart it safely

Press `Ctrl-C` to stop it, then run `./scripts/start.sh` again. This is
always safe: the dashboard's in-memory state resets, but nothing about
your configuration, validation history, or generated reports is lost —
the 90-day validation database (see §13) is a real file on disk, not
something the running process holds only in memory, and it survives a
normal shutdown, a crash, or a full machine restart identically. There
is no special shutdown sequence you need to follow (no "let it finish
writing first") — every write to the validation database is its own
completed transaction before the dashboard ever reports success back to
you, so there is nothing left in an unsafe half-written state to worry
about when you stop it.

## 12. Where reports and exports are stored

Two different things live under this heading:

- **On-demand text reports** (morning scan, weekly review) come from
  the `src/workflows/` modules and are returned as text/data when you
  ask for one — there is no automatic "save every report to a folder"
  step for these. If you want to keep one, save the output somewhere of
  your choosing when you generate it.
- **Validation-cohort exports** (CSV/JSON/XLSX/PDF — validation
  summary, daily portfolio history, trades, strategy performance, risk
  metrics, execution quality, counterfactuals, and more) are produced
  by `src.reporting.export.export_validation_cohort` and written to
  whatever `output_dir` you give it (nothing is exported automatically
  yet — this is a deliberate, on-demand action once the validation
  cohort has real data to report on). By convention this repository's
  own `.gitignore` reserves a local `reports/`/`exports/` folder for
  this at the repository root, so nothing you generate this way is ever
  accidentally committed to version control.

## 13. Where validation results (the database) are stored

The 90-day validation protocol's cohort, daily NAV snapshots, trades,
strategy alternatives ("what else was considered and why it lost"),
Risk Engine decisions, and Fidelity ticket records are all written to a
real SQLite database file on disk — by default `data/options_agent.db`
(configurable via `config/validation.yaml`'s `storage.db_path`, or the
`OPTIONS_AGENT_VALIDATION_DB_PATH` environment variable for an ops-time
override without editing config). This file is never committed to
version control (see `.gitignore`) since it is your own real trading
history, not shared code.

- **How to back it up:** `make backup` (or `./scripts/backup.sh`).
  Writes a timestamped copy to `backups/options_agent_YYYYMMDD_HHMMSS.db`.
  Safe to run at any time, including while the dashboard is running.
- **How to restore it:** `make restore FILE=backups/options_agent_20260921_140000.db`
  (or `./scripts/restore.sh <path>`). This asks you to type `YES` to
  confirm before it touches anything, and it always makes a safety copy
  of whatever database is currently there before overwriting it — it
  never restores automatically or silently.
- **Does it survive a restart?** Yes — this is not a "best effort"
  claim; it is tested directly (see `tests/acceptance
  /test_persistence_restart_recovery.py`), including simulating a
  process crash mid-run and confirming every cohort, snapshot, trade,
  and decision record reconstructs identically afterward.

## 14. Starting the 90-day validation (do this only when you're ready)

**This has not been started yet, and nothing in this README starts it
for you.** The steps below get you to the point of being *ready* to
start it — actually beginning the 90-day clock is a separate, explicit
action you take deliberately, not something that happens as a side
effect of installing or running the software.

1. **Install** the application (§4).
2. **Configure an API key** if you want the AI-assisted proposal/review
   layer (§5) — optional; the deterministic Quant Engine and Risk
   Engine work without it. The Risk Engine's decision is never affected
   either way.
3. **Start the application** (§6).
4. **Verify the V1.0 freeze** by running `make verify-freeze` (or
   `./scripts/verify_freeze.sh`) from a terminal in the project
   directory. This checks that `VALIDATION_MANIFEST.json` exists, that
   nothing material has changed since the platform was frozen, that
   Fidelity is still manual-only, and that live trading is still
   disabled. It should print:
   ```
   PAPER_TRADING_V1.0 / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR VALIDATION INITIALIZATION
   ```
   If it instead reports a failed check, do not proceed — that means
   something in the frozen configuration or code has changed since the
   freeze, and needs a human look before validation begins.
5. **Open the dashboard** (§7) and confirm it loads normally.
6. **Verify the market/data status** shown on the dashboard (or in a
   morning-scan report) looks correct — e.g. it correctly reports
   whether the market is currently open, per `src.data.market_calendar`.
7. **Only when you are explicitly ready** to begin the real 90-day
   clock: that initialization is a separate, deliberately-gated step
   (this repository calls it "Step 23") that has not been built into
   this release. It must be separately authorized by you before any
   code creates a validation cohort, records a Day 1 snapshot, or
   starts counting toward the 90 days — this release intentionally
   stops short of that so you get to make that call with a working,
   verified system in front of you, not a black box.

## 15. How to troubleshoot common problems

- **"ANTHROPIC_API_KEY not set" / AI features fail**: make sure `.env`
  exists (copied from `.env.example`) and has a real key, and that you
  started the app from a shell where that `.env` file was actually
  loaded (`./scripts/start.sh` loads it automatically).
- **Dashboard won't start / "address already in use"**: something else
  is already using port 8000. Either stop that process, or set
  `OPTIONS_AGENT_DASHBOARD_PORT=8001` (or another free port) in `.env`.
- **No opportunities showing up**: the dashboard only shows
  opportunities that have actually been generated and fed to it — it
  does not automatically fetch live market data on its own yet (see
  `progress.md`). This is expected in the current state of the
  project, not a bug.
- **A trade was rejected and you don't know why**: every rejection
  carries a specific, human-readable reason code (e.g. "insufficient
  cash," "stale market data," "exceeds concentration limit") — check
  the dashboard's risk panel or the rejection reason shown on the
  trade.
- **Tests fail after you changed a config file**: the risk limits and
  broker capability files are read literally — a strategy name must
  match exactly, and numeric limits must stay valid (e.g. percentages
  between 0 and 1). Compare against the comments already in
  `config/risk_limits.yaml` / `config/brokers.yaml`.
- **Still stuck**: read `ARCHITECTURE.md` (technical deep dive) and
  `progress.md` (a running log of what's been built and what's known
  to still be missing).

---

## For developers

- `CLAUDE.md` — repository conventions for anyone (human or AI)
  making further changes to this codebase.
- `ARCHITECTURE.md` — full technical architecture.
- `IMPLEMENTATION_PLAN.md` — the phased build plan this project has
  followed.
- `progress.md` — a running log of every implementation step, what was
  built, what was tested, and what remains open.
- `SECURITY_AUDIT.md` — the platform's own security self-audit and
  remediation history.
- `ACCEPTANCE_TEST_REPORT.md` — Step 21's final system integration and
  acceptance test results.
- `STEP_22_FREEZE_REPORT.md` — the PAPER_TRADING_V1.0 pre-validation
  hardening/freeze report, including whether validation has started.
- `VALIDATION_MANIFEST.json` — the frozen version's own machine-checked
  manifest (see §14, step 4).
- Run the test suite with `make test` (or `python -m pytest -q`).
