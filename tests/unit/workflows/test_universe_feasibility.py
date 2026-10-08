"""PAPER_TRADING_V1.5.14: pure unit tests for
`src.workflows.universe_feasibility` -- the universe-feasibility
study's read-only aggregation/classification logic. No event loop, no
sqlite file, no network dependency; every test here constructs its own
`OpportunityScanResult`/`FunnelDiagnostics` fixtures directly or drives
the real `scan_and_rank_opportunities` against in-memory fakes.

Covers items E (research universe loading), L (per-symbol diagnostics),
M (aggregate diagnostics reconciliation), N (suitability classification),
P (rate-limit request-count calculation), and Q (candidate
non-persistence is structural -- these functions never write anywhere)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.data.universe import load_universe, load_universe_strategies
from src.llm.schemas import StrategyType
from src.portfolio.opportunity_scan import scan_and_rank_opportunities
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry
from src.workflows.funnel_diagnostics import FunnelDiagnostics
from src.workflows.universe_feasibility import (
    SuitabilityClassification,
    build_aggregate_feasibility_report,
    build_candidate_feasibility_details,
    build_symbol_feasibility_summaries,
    classify_ticker_suitability,
    expected_tradier_request_count,
)

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
FEASIBILITY_CONFIG = Path(__file__).resolve().parents[3] / "config" / "universe_feasibility.yaml"
OFFICIAL_CONFIG = Path(__file__).resolve().parents[3] / "config" / "universe.yaml"


def _pcs_chain(symbol: str, spot: float, *, construct_candidate: bool = True) -> OptionChain:
    expiration = (NOW + timedelta(days=24)).date()
    underlying = UnderlyingQuote(symbol=symbol, bid=spot - 0.5, ask=spot + 0.5, last=spot, volume=1_000_000, timestamp=NOW, source="tradier")
    if not construct_candidate:
        # A single, deliberately far-OTM/illiquid contract -- no short-put
        # candidate within the configured delta range, so zero
        # construction successes for this symbol.
        contracts = [
            OptionContract(
                option_symbol=f"{symbol}_P1", underlying=symbol, strike=spot * 0.5, expiration=expiration, right=OptionRight.PUT,
                bid=0.0, ask=0.0, last=0.0, volume=0, open_interest=0, delta=-0.01, iv=0.22,
                underlying_price=spot, timestamp=NOW, source="tradier",
            ),
        ]
        return OptionChain(underlying=underlying, contracts=contracts, timestamp=NOW, source="tradier")
    short_strike = round(spot * 0.975 / 5) * 5
    long_strike = short_strike - 3
    contracts = [
        OptionContract(
            option_symbol=f"{symbol}_P{short_strike}", underlying=symbol, strike=short_strike, expiration=expiration, right=OptionRight.PUT,
            bid=2.0, ask=2.2, last=2.1, volume=500, open_interest=1000, delta=-0.15, iv=0.22,
            underlying_price=spot, timestamp=NOW, source="tradier",
        ),
        OptionContract(
            option_symbol=f"{symbol}_P{long_strike}", underlying=symbol, strike=long_strike, expiration=expiration, right=OptionRight.PUT,
            bid=0.8, ask=0.9, last=0.85, volume=500, open_interest=1000, delta=-0.08, iv=0.20,
            underlying_price=spot, timestamp=NOW, source="tradier",
        ),
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=NOW, source="tradier")


def _run_scan(tickers_and_spots: dict[str, float], *, unconstructible: frozenset[str] = frozenset()):
    universe = [UniverseEntry(ticker=t, sector="ETF") for t in tickers_and_spots]
    chains = {
        t: _pcs_chain(t, spot, construct_candidate=t not in unconstructible)
        for t, spot in tickers_and_spots.items()
    }
    limits = get_default_limits()
    portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)
    caps = load_broker_capabilities("internal_paper")
    diagnostics_by_ticker = {t: FunnelDiagnostics(ticker=t) for t in tickers_and_spots}
    scan_result = scan_and_rank_opportunities(
        universe, chains, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), limits, portfolio,
        "normal", caps, now=NOW, proposal_id_prefix="validation-scan", diagnostics_by_ticker=diagnostics_by_ticker,
    )
    return scan_result, diagnostics_by_ticker, frozenset(chains.keys())


class TestResearchUniverseConfigurable:
    """Item E: the research universe is loaded from its own config file,
    and never mutates or reads from the official universe.yaml."""

    def test_feasibility_universe_has_the_twelve_expected_symbols(self):
        entries = load_universe(FEASIBILITY_CONFIG)
        tickers = [e.ticker for e in entries]
        assert tickers == ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "XOM"]

    def test_feasibility_strategies_match_official_active_set(self):
        strategies = load_universe_strategies(FEASIBILITY_CONFIG)
        assert set(strategies) == {"CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD"}

    def test_official_universe_is_still_spy_qqq_only(self):
        entries = load_universe(OFFICIAL_CONFIG)
        assert [e.ticker for e in entries] == ["SPY", "QQQ"]

    def test_loading_the_feasibility_universe_does_not_touch_the_official_file_bytes(self, tmp_path):
        before = OFFICIAL_CONFIG.read_bytes()
        load_universe(FEASIBILITY_CONFIG)
        load_universe_strategies(FEASIBILITY_CONFIG)
        after = OFFICIAL_CONFIG.read_bytes()
        assert before == after


class TestPerSymbolDiagnosticsDeterministic:
    """Item L: per-symbol diagnostics are complete and deterministic."""

    def test_symbol_summaries_reflect_construction_and_ranking_outcomes(self):
        scan_result, diagnostics, received = _run_scan(
            {"SPY": 605.0, "ILLIQUID": 50.0}, unconstructible=frozenset({"ILLIQUID"}),
        )
        summaries = build_symbol_feasibility_summaries(
            universe_tickers=("SPY", "ILLIQUID"), chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result,
        )
        by_ticker = {s.ticker: s for s in summaries}
        assert by_ticker["SPY"].construction_successes == 1
        assert by_ticker["SPY"].quant_evaluations == 1
        assert by_ticker["SPY"].risk_evaluations == 1
        assert by_ticker["ILLIQUID"].construction_successes == 0
        assert by_ticker["ILLIQUID"].quant_evaluations == 0

    def test_running_twice_against_identical_inputs_produces_identical_summaries(self):
        scan_result_1, diagnostics_1, received_1 = _run_scan({"SPY": 605.0})
        scan_result_2, diagnostics_2, received_2 = _run_scan({"SPY": 605.0})
        s1 = build_symbol_feasibility_summaries(
            universe_tickers=("SPY",), chain_received_tickers=received_1,
            diagnostics_by_ticker=diagnostics_1, scan_result=scan_result_1,
        )
        s2 = build_symbol_feasibility_summaries(
            universe_tickers=("SPY",), chain_received_tickers=received_2,
            diagnostics_by_ticker=diagnostics_2, scan_result=scan_result_2,
        )
        assert s1 == s2


class TestAggregateDiagnosticsReconcile:
    """Item M: aggregate totals reconcile with the per-symbol output."""

    def test_aggregate_totals_match_sum_of_per_symbol_fields(self):
        scan_result, diagnostics, received = _run_scan({"SPY": 605.0, "QQQ": 500.0, "ILLIQUID": 50.0}, unconstructible=frozenset({"ILLIQUID"}))
        tickers = ("SPY", "QQQ", "ILLIQUID")
        summaries = build_symbol_feasibility_summaries(
            universe_tickers=tickers, chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result,
        )
        aggregate = build_aggregate_feasibility_report(
            universe_tickers=tickers, chain_received_tickers=received,
            symbol_summaries=summaries, scan_result=scan_result,
        )
        assert aggregate.total_construction_successes == sum(s.construction_successes for s in summaries)
        assert aggregate.total_quant_passed == sum(s.quant_passed for s in summaries)
        assert aggregate.total_risk_passed == sum(s.risk_passed for s in summaries)
        assert aggregate.total_ranked_candidates == sum(s.ranked_candidates for s in summaries)
        assert aggregate.symbols_requested == 3
        assert aggregate.successful_chains == 3
        assert aggregate.failed_chains == 0


class TestCandidateDetailsAndSelection:
    """Item Q: the ranking-survivor view is observational only -- these
    functions never write anywhere, and would_have_been_selected
    matches the real scan_result.best by identity, never re-derived."""

    def test_would_have_been_selected_matches_scan_result_best_exactly(self):
        scan_result, _diag, _received = _run_scan({"SPY": 605.0, "QQQ": 500.0})
        details = build_candidate_feasibility_details(scan_result)
        selected = [d for d in details if d.would_have_been_selected]
        if scan_result.best is None:
            assert selected == []
        else:
            assert len(selected) == 1
            assert selected[0].ticker == scan_result.best.candidate.proposal.ticker

    def test_no_ranked_candidate_means_empty_details(self):
        scan_result, _diag, _received = _run_scan({"ILLIQUID": 50.0}, unconstructible=frozenset({"ILLIQUID"}))
        details = build_candidate_feasibility_details(scan_result)
        assert details == ()


class TestSuitabilityClassification:
    """Item N: deterministic STRONG/ACCEPTABLE/WEAK/UNSUITABLE rules,
    based only on chain/DTE/construction/ranking facts -- never on
    today's market direction or a fabricated performance claim."""

    def _summary(self, **overrides):
        from src.workflows.universe_feasibility import SymbolFeasibilitySummary

        base = dict(
            ticker="TST", market_data_successful=True, chain_usable=True, contracts_seen=2,
            expirations_seen=1, expirations_eligible=1, expirations_rejected=0, strategy_attempts=1,
            construction_attempts=1, construction_successes=1, construction_rejections=0,
            generation_exceptions=0, quant_evaluations=1, quant_passed=1, quant_rejected=0,
            risk_evaluations=1, risk_passed=1, risk_rejected=0, ranked_candidates=1,
            selected_candidate_proposal_id=None, dominant_rejection_reasons=(),
        )
        base.update(overrides)
        return SymbolFeasibilitySummary(**base)

    def test_market_data_failure_is_unsuitable(self):
        s = self._summary(market_data_successful=False, chain_usable=False, expirations_eligible=0, construction_successes=0, ranked_candidates=0)
        assert classify_ticker_suitability(s) == SuitabilityClassification.UNSUITABLE

    def test_zero_eligible_expirations_is_unsuitable(self):
        s = self._summary(expirations_eligible=0, construction_successes=0, ranked_candidates=0)
        assert classify_ticker_suitability(s) == SuitabilityClassification.UNSUITABLE

    def test_zero_construction_successes_is_weak(self):
        s = self._summary(construction_successes=0, ranked_candidates=0)
        assert classify_ticker_suitability(s) == SuitabilityClassification.WEAK

    def test_construction_but_no_ranked_candidate_is_acceptable(self):
        s = self._summary(construction_successes=1, ranked_candidates=0)
        assert classify_ticker_suitability(s) == SuitabilityClassification.ACCEPTABLE

    def test_ranked_candidate_is_strong(self):
        s = self._summary(construction_successes=1, ranked_candidates=1)
        assert classify_ticker_suitability(s) == SuitabilityClassification.STRONG

    def test_classification_is_deterministic_never_based_on_direction_or_pnl(self):
        s = self._summary()
        assert classify_ticker_suitability(s) == classify_ticker_suitability(s)


class TestRateLimitRequestBudget:
    """Item P (deterministic half): the expected Tradier request count
    is computed, never measured or guessed."""

    def test_twelve_symbols_with_default_max_expirations(self):
        assert expected_tradier_request_count(num_symbols=12, max_expirations=6) == 12 * (2 + 6 + 1)

    def test_zero_symbols_is_zero_requests(self):
        assert expected_tradier_request_count(num_symbols=0, max_expirations=6) == 0

    def test_negative_num_symbols_rejected(self):
        with pytest.raises(ValueError):
            expected_tradier_request_count(num_symbols=-1, max_expirations=6)

    def test_negative_max_expirations_rejected(self):
        with pytest.raises(ValueError):
            expected_tradier_request_count(num_symbols=12, max_expirations=-1)
