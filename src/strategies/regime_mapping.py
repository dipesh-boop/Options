"""Candidate-generation guidelines mapping a market/volatility view to
MULTIPLE viable strategy candidates — explicitly NOT automatic trading
rules and explicitly NOT a one-regime-to-one-strategy mapping. Every
mapping below is the exact guideline table Step 19A specifies. The
Strategy Competition Engine (`src.strategies.selector`) uses this only
to decide which strategies are even WORTH constructing and pricing —
every constructed candidate is still independently quant-priced,
red-teamed, and risk-gated before anything is selected; nothing here
decides a trade.
"""
from __future__ import annotations

from enum import Enum

from src.strategies.base import StrategyKind

_CC, _CSP, _PCS = StrategyKind.COVERED_CALL, StrategyKind.CASH_SECURED_PUT, StrategyKind.PUT_CREDIT_SPREAD
_CCS = StrategyKind.CALL_CREDIT_SPREAD
_BCS, _BPS = StrategyKind.BULL_CALL_SPREAD, StrategyKind.BEAR_PUT_SPREAD
_LC, _LP = StrategyKind.LONG_CALL, StrategyKind.LONG_PUT
_PP, _PCOL = StrategyKind.PROTECTIVE_PUT, StrategyKind.PROTECTIVE_COLLAR
_STRD, _STRN = StrategyKind.LONG_STRADDLE, StrategyKind.LONG_STRANGLE
_IC, _IB = StrategyKind.SHORT_IRON_CONDOR, StrategyKind.SHORT_IRON_BUTTERFLY


class MarketView(str, Enum):
    STRONGLY_BULLISH = "strongly_bullish"
    MODERATELY_BULLISH = "moderately_bullish"
    NEUTRAL_RANGE_BOUND = "neutral_range_bound"
    MODERATELY_BEARISH = "moderately_bearish"
    STRONGLY_BEARISH = "strongly_bearish"
    LARGE_MOVE_EXPECTED = "large_move_expected"  # volatility expansion, direction uncertain
    PORTFOLIO_PROTECTION = "portfolio_protection"
    HIGH_IV_CONTRACTION_EXPECTED = "high_iv_contraction_expected"


# Order within each tuple is not a ranking -- src.strategies.comparison
# ranks the actually-priced candidates; this table only decides which
# strategies are worth pricing at all for a given view.
CANDIDATE_STRATEGIES_BY_VIEW: dict[MarketView, tuple[StrategyKind, ...]] = {
    MarketView.MODERATELY_BULLISH: (_CC, _CSP, _BCS, _PCS, _LC),
    MarketView.STRONGLY_BULLISH: (_BCS, _LC, _PCS),
    MarketView.MODERATELY_BEARISH: (_BPS, _LP, _CCS, _PP),
    MarketView.STRONGLY_BEARISH: (_LP, _BPS),
    MarketView.NEUTRAL_RANGE_BOUND: (_IC, _IB, _CC),
    MarketView.LARGE_MOVE_EXPECTED: (_STRD, _STRN),
    MarketView.PORTFOLIO_PROTECTION: (_PP, _PCOL),
    # "Evaluate... but ONLY when directional, tail and event risks are
    # acceptable" -- that acceptability check is the Devil's Advocate's
    # and Risk Engine's job downstream, never decided by this table.
    MarketView.HIGH_IV_CONTRACTION_EXPECTED: (_IC, _IB, _PCS, _CCS),
}

# Strategies that require existing shares before they can even be
# constructed -- src.strategies.suitability filters these out entirely
# when the portfolio doesn't hold the underlying, so "do not incorrectly
# classify a short call as a Covered Call" is structural, not a review
# step: a bare short call is never even offered as COVERED_CALL unless
# shares are actually on record.
SHARE_REQUIRING_STRATEGIES: frozenset[StrategyKind] = frozenset({_CC, _PP, _PCOL})


def candidate_strategies_for(view: MarketView) -> tuple[StrategyKind, ...]:
    return CANDIDATE_STRATEGIES_BY_VIEW[view]
