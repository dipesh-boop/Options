"""Step 14B's own final system test: the full pipeline

    MARKET DATA -> MARKET REGIME -> UNDERLYING OPPORTUNITY
    -> GENERATE ALL COMPATIBLE STRATEGIES -> PYTHON QUANT ANALYSIS
    -> STRATEGY COMPARISON -> PORTFOLIO IMPACT
    -> Devil's Advocate / Portfolio Manager verdicts (supplied, same
       "no real API key needed" pattern every other pipeline test in
       this codebase already uses -- see test_system_integration.py)
    -> DETERMINISTIC RISK-ADJUSTED SELECTION -> BEST CANDIDATE OR
       NO_TRADE

run against the 8 named scenarios: STRONG BULL, MODERATE BULL,
SIDEWAYS LOW VOL, SIDEWAYS HIGH IV, STRONG BEAR, VOLATILITY EXPANSION,
VOLATILITY CONTRACTION, PORTFOLIO CRASH.

Deliberately never asserts which strategy wins in any scenario -- Step
14B's own instruction ("Do not write tests requiring a specific
strategy to always win for a given regime") -- only that the pipeline
produces a valid candidate or NO_TRADE, that every candidate obeys its
own defined-risk bound against the scenario's simulated terminal
prices, and that NO_TRADE only wins when nothing cleared the hurdle.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.backtest.regime_scenarios import RegimeScenario, generate_regime_path
from src.data.option_chain import OptionContract, OptionRight as DataOptionRight
from src.quant.black_scholes import price as bs_price
from src.quant.monte_carlo import payoff_at_expiration, payoff_profile
from src.risk.reason_codes import RiskDecision
from src.strategies.bear_put_spread import evaluate_bear_put_spread
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.call_credit_spread import evaluate_call_credit_spread
from src.strategies.cash_secured_put import evaluate_cash_secured_put
from src.strategies.covered_call import evaluate_covered_call
from src.strategies.long_call import evaluate_long_call
from src.strategies.long_put import evaluate_long_put
from src.strategies.long_straddle import evaluate_long_straddle
from src.strategies.long_strangle import evaluate_long_strangle
from src.strategies.protective_collar import evaluate_protective_collar
from src.strategies.protective_put import evaluate_protective_put
from src.strategies.put_credit_spread import evaluate_put_credit_spread
from src.strategies.regime_mapping import MarketView, candidate_strategies_for
from src.strategies.selector import CandidateVerdicts, select_best_or_no_trade
from src.strategies.suitability import filter_suitable_strategies
from src.strategies.base import StrategyKind

from .conftest import EXPIRATION, SPOT, limits, portfolio, portfolio_with_shares

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
RATE = 0.04
DAYS_TO_EXPIRY = 30
T = DAYS_TO_EXPIRY / 365


def _bs_contract(strike: float, right: DataOptionRight, spot: float, sigma: float) -> OptionContract:
    """A contract priced by the real quant engine at the given
    spot/sigma, not a hand-picked number -- so every one of the 8
    scenarios' differing volatility levels produces internally
    consistent, realistic premiums for whichever candidates it prices."""
    from src.quant.black_scholes import OptionRight as QuantOptionRight

    quant_right = QuantOptionRight.CALL if right == DataOptionRight.CALL else QuantOptionRight.PUT
    mid = max(bs_price(spot, strike, T, RATE, sigma, quant_right), 0.05)
    return OptionContract(
        underlying="XYZ", option_symbol=f"XYZ{strike}{right.value}", expiration=EXPIRATION, strike=strike,
        right=right, bid=round(mid * 0.98, 4), ask=round(mid * 1.02, 4), last=mid, volume=500, open_interest=1000,
        iv=sigma, underlying_price=spot, timestamp=NOW, source="test",
    )


class _Chain:
    """Fixed strike ladder around `spot`, repriced fresh per scenario at
    that scenario's own sigma -- enough strikes to construct every
    constructible Tier1 strategy this test exercises."""

    def __init__(self, spot: float, sigma: float):
        self.spot = spot
        self.sigma = sigma
        self.puts = {k: _bs_contract(k, DataOptionRight.PUT, spot, sigma) for k in (85, 90, 95, 100, 105)}
        self.calls = {k: _bs_contract(k, DataOptionRight.CALL, spot, sigma) for k in (95, 100, 105, 110, 115)}


def _evaluate_candidates(kinds: tuple[StrategyKind, ...], chain: _Chain, has_shares: bool):
    """Prices whichever of `kinds` this test knows how to construct from
    `chain` -- the same "construct only what suitability already
    allows" discipline `src.strategies.selector`'s own docstring
    describes; a kind this helper can't build (no shares-requiring
    covered call without shares, or a Tier2 3-4 leg structure) is simply
    skipped, exactly like a real caller with an incomplete chain would."""
    common = dict(
        ticker="XYZ", expiration=EXPIRATION, spot=chain.spot, sigma=chain.sigma, t=T, rate=RATE,
        days_to_expiry=DAYS_TO_EXPIRY, num_contracts=1, limits=limits(),
    )
    out = []
    for kind in kinds:
        if kind == StrategyKind.CASH_SECURED_PUT:
            out.append(evaluate_cash_secured_put(put_contract=chain.puts[95], **common))
        elif kind == StrategyKind.COVERED_CALL and has_shares:
            out.append(evaluate_covered_call(call_contract=chain.calls[105], cost_basis=95.0, **common))
        elif kind == StrategyKind.PUT_CREDIT_SPREAD:
            out.append(evaluate_put_credit_spread(short_put_contract=chain.puts[95], long_put_contract=chain.puts[90], **common))
        elif kind == StrategyKind.CALL_CREDIT_SPREAD:
            out.append(evaluate_call_credit_spread(short_call_contract=chain.calls[105], long_call_contract=chain.calls[110], **common))
        elif kind == StrategyKind.BULL_CALL_SPREAD:
            out.append(evaluate_bull_call_spread(long_call_contract=chain.calls[100], short_call_contract=chain.calls[110], **common))
        elif kind == StrategyKind.BEAR_PUT_SPREAD:
            out.append(evaluate_bear_put_spread(long_put_contract=chain.puts[100], short_put_contract=chain.puts[90], **common))
        elif kind == StrategyKind.LONG_CALL:
            out.append(evaluate_long_call(call_contract=chain.calls[100], **common))
        elif kind == StrategyKind.LONG_PUT:
            out.append(evaluate_long_put(put_contract=chain.puts[100], **common))
        elif kind == StrategyKind.LONG_STRADDLE:
            out.append(evaluate_long_straddle(call_contract=chain.calls[100], put_contract=chain.puts[100], **common))
        elif kind == StrategyKind.LONG_STRANGLE:
            out.append(evaluate_long_strangle(call_contract=chain.calls[105], put_contract=chain.puts[95], **common))
        elif kind == StrategyKind.PROTECTIVE_PUT and has_shares:
            out.append(evaluate_protective_put(put_contract=chain.puts[90], cost_basis=95.0, **common))
        elif kind == StrategyKind.PROTECTIVE_COLLAR and has_shares:
            out.append(evaluate_protective_collar(call_contract=chain.calls[110], put_contract=chain.puts[90], cost_basis=95.0, **common))
        # Every Tier2 3-4 leg kind (iron condor/butterfly, long call
        # butterfly) is left unpriced here -- this helper only builds
        # 1-2 leg structures, exactly the leg-cap boundary
        # ARCHITECTURE.md §13 documents.
    return out


_SCENARIOS = [
    ("STRONG_BULL", MarketView.STRONGLY_BULLISH, 0.22, RegimeScenario.BULL, False),
    ("MODERATE_BULL", MarketView.MODERATELY_BULLISH, 0.20, RegimeScenario.BULL, False),
    # NEUTRAL_RANGE_BOUND's own candidate list (iron condor, iron
    # butterfly, covered call) is mostly Tier2 (evaluation-only, not
    # constructed by this test's helper) plus a shares-requiring
    # strategy -- realistic only against a portfolio that already holds
    # the underlying, same as PORTFOLIO_CRASH below.
    ("SIDEWAYS_LOW_VOL", MarketView.NEUTRAL_RANGE_BOUND, 0.15, RegimeScenario.LOW_VOLATILITY, True),
    ("SIDEWAYS_HIGH_IV", MarketView.HIGH_IV_CONTRACTION_EXPECTED, 0.38, RegimeScenario.HIGH_VOLATILITY, False),
    ("STRONG_BEAR", MarketView.STRONGLY_BEARISH, 0.28, RegimeScenario.BEAR, False),
    ("VOLATILITY_EXPANSION", MarketView.LARGE_MOVE_EXPECTED, 0.18, RegimeScenario.VOLATILITY_EXPANSION, False),
    ("VOLATILITY_CONTRACTION", MarketView.NEUTRAL_RANGE_BOUND, 0.30, RegimeScenario.VOLATILITY_CONTRACTION, True),
    ("PORTFOLIO_CRASH", MarketView.PORTFOLIO_PROTECTION, 0.45, RegimeScenario.MARKET_CRASH, True),
]


class TestFinalSystemScenarios:
    @pytest.mark.parametrize("name,view,sigma,regime,needs_shares", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
    def test_full_pipeline_selects_a_candidate_or_no_trade(self, name, view, sigma, regime, needs_shares):
        # ---- MARKET DATA / REGIME / OPPORTUNITY ----
        chain = _Chain(spot=SPOT, sigma=sigma)
        port = portfolio_with_shares() if needs_shares else portfolio()

        # ---- GENERATE ALL COMPATIBLE STRATEGIES ----
        suitable = filter_suitable_strategies(candidate_strategies_for(view), ticker="XYZ", portfolio=port, contracts=1)
        candidates = _evaluate_candidates(suitable, chain, has_shares=needs_shares)
        assert len(candidates) >= 1, f"{name}: expected at least one constructible candidate"

        # ---- PYTHON QUANT ANALYSIS is already inside each evaluate_* call ----
        for ev in candidates:
            assert ev.maximum_loss >= 0
            assert ev.capital_requirement >= 0

        # ---- Devil's Advocate / Portfolio Manager verdicts (supplied) ----
        verdicts = {
            ev.strategy_kind: CandidateVerdicts(False, "sound thesis for this scenario", RiskDecision.APPROVE, "approved")
            for ev in candidates
        }

        # ---- STRATEGY COMPARISON + PORTFOLIO IMPACT + SELECTION ----
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=port, limits=limits(), spot=SPOT)

        # The falsifiable property: a valid outcome exists (a real
        # candidate that was actually generated, or NO_TRADE) --
        # deliberately never which one.
        assert len(outcome.comparison_table) == len(candidates)
        if outcome.selected is not None:
            assert outcome.selected.strategy_kind in suitable
        else:
            assert outcome.selection_reason  # NO_TRADE always carries a reason, never a silent None

        # ---- Cross-check: every candidate's own defined-risk bound
        # holds against this scenario's simulated terminal prices --
        # same property test_regime_scenarios.py already proves, run
        # here against the scenario's actually-generated candidates
        # rather than one hand-picked position. Unbounded max_profit is
        # fine (long premium); max_loss must never be exceeded.
        path = generate_regime_path(regime, start_price=SPOT, num_days=30, seed=7)
        for ev in candidates:
            profile = payoff_profile(ev.position)
            for terminal_price in path.prices:
                pnl = payoff_at_expiration(ev.position, terminal_price)
                assert pnl >= -profile.max_loss - 1e-6, (
                    f"{name}/{ev.strategy_kind.value}: pnl {pnl} exceeded max_loss bound {profile.max_loss}"
                )

    def test_no_trade_is_a_reachable_outcome_when_nothing_clears_the_hurdle(self):
        """NO_TRADE must be reachable, not merely representable -- an
        artificially impossible hurdle forces every candidate to lose,
        proving the selector can actually return it end to end."""
        chain = _Chain(spot=SPOT, sigma=0.22)
        port = portfolio()
        suitable = filter_suitable_strategies(candidate_strategies_for(MarketView.MODERATELY_BULLISH), ticker="XYZ", portfolio=port, contracts=1)
        candidates = _evaluate_candidates(suitable, chain, has_shares=False)
        verdicts = {
            ev.strategy_kind: CandidateVerdicts(False, "sound thesis", RiskDecision.APPROVE, "approved") for ev in candidates
        }
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=port, limits=limits(), spot=SPOT, no_trade_hurdle=1e9)
        assert outcome.selected is None
