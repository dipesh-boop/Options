"""Step 22.3: the deterministic Strategy Lifecycle Management Engine.

This package manages every position from ENTRY -> ACTIVE -> MONITORING
-> MANAGEMENT DECISION -> EXIT/EXPIRATION/ASSIGNMENT/ADJUSTMENT ->
POST-TRADE ANALYSIS, for every strategy this platform supports
(including the Wheel, via `src.wheel` -- this package never duplicates
that package's own state machine, it delegates to it).

**The lifecycle engine remains subordinate to the deterministic Risk
Engine**, exactly like every other package in this codebase. Claude/LLM
may analyze and recommend (via `src.llm.devils_advocate`/
`portfolio_manager`, called by whatever orchestrates a lifecycle-driven
proposal, outside this package); Python performs every calculation;
deterministic rules (`src.lifecycle.triggers`/`precedence`) determine
mandatory actions; `src.risk.engine.evaluate_trade_proposal` retains
final veto authority over any new/adjusted/rolled position this package
proposes. `PaperBroker` performs simulation
(`src.lifecycle.paper_events`); Fidelity remains human manual execution
only (`src.lifecycle.fidelity_events`). No module in this package places,
cancels, or modifies a live brokerage order, and none imports a live
trading client of any kind.

**Core principle (Part 1): STRATEGY is separate from MANAGEMENT POLICY.**
`PUT_CREDIT_SPREAD` (a `StrategyKind`) is the structure; `PCS_50PCT_21DTE`
and `PCS_HOLD_TO_EXPIRY` (two `ManagementPolicy` instances,
`src.lifecycle.policies_library`) are different, independently
researchable ways to manage that exact same structure. Nothing in this
package assumes one management policy is universally superior to
another -- every named policy in `policies_library.py` is documented as
a RESEARCH DEFAULT, never as "the correct" configuration.
"""
