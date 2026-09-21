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

## 11. How to restart it

Press `Ctrl-C` to stop it, then run `./scripts/start.sh` again. Nothing
about your configuration or historical data is lost between restarts —
the dashboard's in-memory state resets, but the underlying reports,
validation records, and config files on disk are untouched.

## 12. Where reports are stored

Daily, weekly, and monthly reports are generated on demand by the
`src/workflows/` modules (morning scan, weekly review) and returned as
text/data — there is currently no automatic "save every report to a
folder" step. If you want to keep a report, save the output somewhere
of your choosing when you generate it.

## 13. Where validation results are stored

The 90-day validation protocol's trades, snapshots, and scorecards are
held by the `ValidationStore` (see `src/validation/session.py`) for the
process's lifetime. As with reports, persisting this to a database file
on disk for long-term, cross-restart tracking is a known next step, not
yet wired up automatically — see `progress.md`'s "Next up" section for
the current status.

## 14. How to troubleshoot common problems

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
- Run the test suite with `make test` (or `python -m pytest -q`).
