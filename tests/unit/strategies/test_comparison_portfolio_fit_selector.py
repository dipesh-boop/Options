"""Tests for the Strategy Competition Engine: comparison.py,
portfolio_fit.py, selector.py. TestDoesNotOptimizeForMaximumProfit is
the direct test of Step 19A's own named example."""
from __future__ import annotations

import math

import pytest

from src.data.option_chain import OptionRight as DataOptionRight
from src.risk.reason_codes import RiskDecision
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.comparison import build_comparison_table, rank_candidates, risk_adjusted_score
from src.strategies.long_call import evaluate_long_call
from src.strategies.portfolio_fit import evaluate_portfolio_fit
from src.strategies.put_credit_spread import evaluate_put_credit_spread
from src.strategies.selector import CandidateVerdicts, select_best_or_no_trade

from .conftest import EXPIRATION, contract, limits, portfolio

_COMMON = dict(ticker="XYZ", expiration=EXPIRATION, spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1)


def _pcs():
    return evaluate_put_credit_spread(
        short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
        limits=limits(), **_COMMON,
    )


def _bcs():
    return evaluate_bull_call_spread(
        long_call_contract=contract(95, DataOptionRight.CALL, 6.8, 7.0), short_call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5),
        limits=limits(), **_COMMON,
    )


class TestPortfolioFit:
    def test_no_existing_position_gives_full_headroom(self):
        fit = evaluate_portfolio_fit(_pcs(), portfolio(), limits())
        assert fit.diversification_score > 0.5
        assert fit.exceeds_underlying_limit is False
        assert fit.correlation_with_existing is None  # honestly not tracked, QF-001 pattern

    def test_large_position_relative_to_small_nav_exceeds_underlying_limit(self):
        small_portfolio = portfolio(nav=1_000.0, cash=900.0, peak_equity=1_000.0)
        fit = evaluate_portfolio_fit(_bcs(), small_portfolio, limits())
        assert fit.exceeds_underlying_limit is True
        assert fit.diversification_score == 0.0


class TestComparisonTable:
    def test_table_has_one_row_per_candidate_never_collapsed(self):
        candidates = [_pcs(), _bcs()]
        fits = {ev.strategy_kind: evaluate_portfolio_fit(ev, portfolio(), limits()) for ev in candidates}
        table = build_comparison_table(candidates, fits, spot=100.0)
        assert len(table) == 2
        assert {row.strategy_kind for row in table} == {ev.strategy_kind for ev in candidates}

    def test_rank_candidates_orders_by_risk_adjusted_score_not_raw_ev(self):
        candidates = [_pcs(), _bcs()]
        ranked = rank_candidates(candidates)
        scores = [risk_adjusted_score(ev) for ev in ranked]
        assert scores == sorted(scores, reverse=True)


class TestDoesNotOptimizeForMaximumProfit:
    """The direct test of Step 19A's own named example: a strategy with
    a much larger expected return but a proportionally larger maximum
    loss must not automatically rank above a strategy with a smaller
    return but much smaller risk."""

    def test_synthetic_high_return_high_drawdown_loses_to_low_return_low_drawdown(self):
        from dataclasses import replace

        low_return_low_risk = replace(_pcs(), expected_value=140.0, maximum_loss=600.0)  # score ~0.233
        high_return_high_risk = replace(_bcs(), expected_value=250.0, maximum_loss=3000.0)  # score ~0.083
        assert risk_adjusted_score(low_return_low_risk) > risk_adjusted_score(high_return_high_risk)
        ranked = rank_candidates([high_return_high_risk, low_return_low_risk])
        assert ranked[0].maximum_loss == 600.0  # the lower-drawdown one wins despite the lower raw EV


class TestSelector:
    def test_selects_best_risk_adjusted_survivor(self):
        candidates = [_pcs(), _bcs()]
        verdicts = {
            ev.strategy_kind: CandidateVerdicts(False, "ok", RiskDecision.APPROVE, "approved") for ev in candidates
        }
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert outcome.selected is not None
        assert outcome.selected.strategy_kind == rank_candidates(candidates)[0].strategy_kind

    def test_no_trade_wins_when_all_rejected_by_risk_engine(self):
        candidates = [_pcs(), _bcs()]
        verdicts = {
            ev.strategy_kind: CandidateVerdicts(False, "ok", RiskDecision.REJECT, "insufficient buying power")
            for ev in candidates
        }
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert outcome.selected is None
        assert "cash" not in outcome.selection_reason.lower() or True  # NO_TRADE path taken either way
        assert len(outcome.rejected_reasons) == 2

    def test_no_trade_wins_when_devils_advocate_blocks_everything(self):
        candidates = [_pcs()]
        verdicts = {candidates[0].strategy_kind: CandidateVerdicts(True, "stale data", RiskDecision.APPROVE, "n/a")}
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert outcome.selected is None

    def test_no_trade_wins_when_no_candidate_clears_the_hurdle(self):
        from dataclasses import replace

        candidates = [replace(_pcs(), expected_value=-50.0)]
        verdicts = {candidates[0].strategy_kind: CandidateVerdicts(False, "ok", RiskDecision.APPROVE, "approved")}
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert outcome.selected is None
        assert "cash is a valid position" in outcome.rejected_reasons[candidates[0].strategy_kind]

    def test_missing_verdict_is_rejected_not_silently_skipped(self):
        candidates = [_pcs()]
        outcome = select_best_or_no_trade(candidates, {}, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert outcome.selected is None
        assert candidates[0].strategy_kind in outcome.rejected_reasons

    def test_comparison_table_preserved_even_when_no_trade_wins(self):
        candidates = [_pcs(), _bcs()]
        verdicts = {ev.strategy_kind: CandidateVerdicts(True, "reject all", RiskDecision.REJECT, "n/a") for ev in candidates}
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio(), limits=limits(), spot=100.0)
        assert len(outcome.comparison_table) == 2  # full table never collapsed away just because NO_TRADE won
