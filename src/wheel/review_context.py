"""Part 17/18's Wheel-specific review checklists, as plain Python data,
plus the deterministic assembler that turns a `WheelPosition` into a
`src.llm.context.WheelReviewContext` for `src.llm.devils_advocate`/
`src.llm.portfolio_manager`. Nothing here calls a model — this is
reference content and Python-computed figures only, handed to those two
existing orchestration modules as read-only context exactly the way
`MarketRegimeAssessment`/`QuantitativeAnalysisContext` already are.

Note on `DevilsAdvocateReview.failure_scenarios`: that field already
requires `min_length=3` for *every* review, Wheel or not (see
`src.llm.schemas`) — Part 17's "at least three relevant failure modes"
is therefore already a structural guarantee before this module exists.
What this module adds is the Wheel-specific reference list the model is
expected to actually draw from and reason about for a Wheel proposal,
not a new schema requirement.
"""
from __future__ import annotations

from datetime import datetime

from src.llm.context import WheelReviewContext
from src.wheel.models import WheelPosition
from src.wheel.state import WheelState

# Part 17, verbatim (10 named failure modes).
WHEEL_DEVILS_ADVOCATE_FAILURE_PROMPTS: tuple[str, ...] = (
    "The underlying collapses after the put is assigned -- how large is the unrealized loss, "
    "and does the thesis still hold at this lower price?",
    "Repeated covered-call premium fails to offset the stock's own loss -- is the Wheel actually "
    "recovering, or is the premium merely slowing the bleed while basis erosion continues?",
    "A covered call caps recovery during a sharp rebound -- shares are called away (or capped) "
    "right as the position would otherwise have recovered.",
    "Volatility expands sharply after assignment -- the position is now larger, riskier, and "
    "harder to exit than it looked at entry.",
    "Concentration increases because of assignment -- shares now count toward underlying/sector "
    "exposure in a way the original CSP's cash reservation did not.",
    "Liquidity deteriorates in the underlying or its options after assignment, making an orderly "
    "exit (of the shares, or of a subsequent covered call) harder than it was at entry.",
    "Assignment occurs at an especially adverse moment -- just before a known or suspected event, "
    "or during a broad market dislocation.",
    "A covered call sold below basis crystallizes a loss on assignment that a covered call above "
    "basis would not have.",
    "Opportunity cost of the cash-secured collateral (or the shares' own committed capital) -- what "
    "else could this capital have earned, and is the Wheel's premium actually compensating for that?",
    "Repeated premium collection creates a false appearance of safety -- a long run of small, "
    "consistent credits can mask a large, rarely-realized tail loss sitting underneath it.",
)

# Part 18, verbatim, split into the CSP-phase and CC-phase question sets.
WHEEL_PM_CSP_PHASE_QUESTIONS: tuple[str, ...] = (
    "Why do we want to own this underlying?",
    "Why now?",
    "Why a cash-secured put rather than simply buying shares?",
    "Why this strike?",
    "Why this expiration?",
    "What happens if assigned tomorrow?",
    "Would we still want the stock after a 20% decline?",
    "What is the opportunity cost of the reserved cash?",
    "What invalidates the thesis?",
    "How correlated is this exposure with the rest of the portfolio?",
    "Is the premium adequate compensation for the downside?",
    "Is CASH superior to taking this trade at all?",
)
WHEEL_PM_CC_PHASE_QUESTIONS: tuple[str, ...] = (
    "Why sell upside now?",
    "What happens if the stock rallies sharply?",
    "Are we comfortable losing the shares at this strike?",
    "Is the strike below basis?",
    "Is the premium adequate for the upside surrendered?",
    "Would holding the stock without a covered call be preferable right now?",
)


def build_wheel_review_context(
    wheel: WheelPosition, *, is_new_wheel_candidate: bool, now: datetime,
) -> WheelReviewContext:
    """`is_new_wheel_candidate` distinguishes the CSP entry phase (Part
    18's first question set) from the covered-call phase (Part 18's
    second set) -- the caller knows which proposal is under review and
    supplies this explicitly rather than this function guessing from
    `wheel.state` alone (a Wheel at `CC_ELIGIBLE` is unambiguous, but the
    caller's own intent -- "I am about to propose a CSP for a fresh
    wheel_id" vs. "I am about to propose a covered call" -- is the more
    reliable signal and costs nothing to just pass in)."""
    acc = wheel.accounting
    open_cc = wheel.open_cc_cycle
    below_acq = bool(open_cc.below_acquisition_basis) if open_cc is not None else False
    below_econ = bool(open_cc.below_economic_basis) if open_cc is not None else False
    questions = WHEEL_PM_CSP_PHASE_QUESTIONS if is_new_wheel_candidate else WHEEL_PM_CC_PHASE_QUESTIONS

    return WheelReviewContext(
        wheel_id=wheel.wheel_id, ticker=wheel.ticker, state=wheel.state.value,
        csp_cycle_count=len(wheel.csp_cycles), cc_cycle_count=len(wheel.cc_cycles),
        shares_owned=acc.shares_owned, acquisition_basis_per_share=acc.acquisition_basis_per_share,
        economic_basis_per_share=acc.economic_basis_per_share, capital_committed=acc.capital_committed,
        total_net_pnl=acc.total_csp_premium + acc.total_cc_premium - acc.total_commissions + acc.realized_stock_pnl,
        below_acquisition_basis=below_acq, below_economic_basis=below_econ,
        is_new_wheel_candidate=is_new_wheel_candidate,
        wheel_specific_failure_prompts=WHEEL_DEVILS_ADVOCATE_FAILURE_PROMPTS,
        portfolio_manager_questions=questions,
    )
