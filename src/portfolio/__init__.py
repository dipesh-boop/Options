"""Step 22.4: the deterministic Portfolio Control Loop.

This package is the orchestration layer that continuously refreshes
market data, revalues the PAPER portfolio, monitors every open position,
evaluates lifecycle/risk triggers, scans for new opportunities, and
produces auditable, deterministic recommendations -- it never becomes a
second Risk Engine or a second Lifecycle Engine. Every authoritative
number and every allow/reject decision is still produced by
`src.quant`/`src.risk`/`src.lifecycle`; this package's own code is
orchestration, aggregation, and presentation on top of those, exactly as
CLAUDE.md's invariant hierarchy requires:

MARKET DATA -> CANONICAL DATA MODELS -> PYTHON QUANT ENGINE ->
LIFECYCLE ENGINE -> PORTFOLIO CONTROL LOOP -> DETERMINISTIC RISK ENGINE
-> RECOMMENDATION -> HUMAN -> FIDELITY MANUAL EXECUTION -> HUMAN-CONFIRMED
FILL -> PORTFOLIO RECONCILIATION.

No module in this package places, previews, modifies, or cancels an
order, and none imports a live trading client of any kind -- the same
invariant every other package in this codebase upholds.
"""
