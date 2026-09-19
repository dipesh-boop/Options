# Progress

Living status tracker for the systematic options research & paper-trading
platform. Update this file at the end of every work session — append,
don't rewrite history.

## Status: Phase 0 — design deliverables complete, foundations not started

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
  `src/options_platform/risk/greeks.py` and is worth porting rather than
  rewriting when Phase 0/1 starts.
- Nothing has been committed to git yet this session; working tree has the
  three planning docs plus the untouched `app/` prototype.

## Open decisions carried forward (see ARCHITECTURE.md §10)

- [ ] Historical options data vendor for backtesting (Phase 2 blocker)
- [ ] Schwab paper-trading / sandbox capability (Phase 4 blocker) — needs
      research against current Schwab developer docs, not assumed
- [ ] Sector/classification data source for correlation/concentration checks
- [ ] Final ~50-name equity universe list + liquidity criteria
- [ ] What happens to the pre-existing `app/` prototype (port vs. delete)

## Next up

- Awaiting review/approval of `ARCHITECTURE.md` and `IMPLEMENTATION_PLAN.md`
  before starting Phase 0 (repo scaffolding, DB schema, CI, and a decision
  on the `app/` prototype).
