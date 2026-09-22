"""The stateful Wheel strategy (Step 22.2).

A Wheel is not a single order — it is a persistent, multi-stage position
lifecycle: sell a cash-secured put, either let it expire/buy it back or
get assigned 100 shares per contract, then (once holding shares) sell
covered calls against them until the shares are called away or the
position is manually exited. Every stage still goes through the exact
same deterministic Python Quant -> deterministic Risk Engine ->
PaperBroker/Fidelity pipeline every other strategy in this platform uses
— the Wheel adds a state machine and cross-cycle accounting layer on top
of that pipeline, never a shortcut around it.

**The Wheel never becomes its own order type.** Every order a Wheel
places is a completely ordinary `TradeProposal` with
`strategy=StrategyType.CASH_SECURED_PUT` or
`strategy=StrategyType.COVERED_CALL` — the exact same two strategy types
this platform has supported since Step 9, unchanged. `src.wheel` tracks
which `wheel_id` a given CSP/CC belongs to and what state that Wheel is
in; it does not add a new leg shape, a new Risk Engine code path, or a
new PaperBroker order type. See `src/wheel/state.py`'s module docstring
for the state machine and `CLAUDE.md`'s non-negotiable invariants, all of
which apply to a Wheel's orders exactly as they apply to a standalone CSP
or covered call.

Plain-English summary for the owner: the Wheel is not a guaranteed-income
strategy. Selling a cash-secured put exchanges option premium for the
obligation to buy 100 shares per contract at the strike if assigned —
that is real equity downside exposure, not merely "a strike distance
away." A string of collected premiums can create a false sense of safety
right up until an assignment during a sharp decline, after which selling
covered calls caps the recovery while the stock is held. The Wheel may
underperform simply buying and holding the same underlying, or simply
holding cash, over any given stretch — premium received does not
eliminate stock downside risk, it only partially offsets it."""
