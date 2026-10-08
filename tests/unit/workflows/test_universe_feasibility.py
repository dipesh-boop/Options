"""PAPER_TRADING_V1.5.14: pure unit tests for
`src.workflows.universe_feasibility` -- the universe-feasibility
study's read-only aggregation/classification logic. No event loop, no
sqlite file, no network dependency; every test here constructs its own
`OpportunityScanResult`/`FunnelDiagnostics` fixtures directly, drives
the real `scan_and_rank_opportunities` against in-memory fakes, or
builds a plain `RateLimitState` dataclass instance.

Covers items E (research universe loading), L (per-symbol diagnostics),
M (aggregate diagnostics reconciliation), N/G (structural suitability
vs. today's opportunity), P (rate-limit request-count/headroom
calculation), Q (candidate non-persistence is structural -- these
functions never write anywhere), and the V1.5.14 acceptance correction's
corrected request-budget formula and headroom/batching helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.data.rate_limiter import RateLimitState
from src.data.universe import load_universe, load_universe_strategies
from src.llm.schemas import StrategyType
from src.portfolio.opportunity_scan import scan_and_rank_opportunities
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry
from src.workflows.funnel_diagnostics import FunnelDiagnostics
from src.workflows.universe_feasibility import (
    CorrelationSymbolStatus,
    OpportunityToday,
    RateLimitSafetyConfigError,
    SkipReason,
    StructuralSuitability,
    SymbolFeasibilitySummary,
    build_aggregate_feasibility_report,
    build_candidate_feasibility_details,
    build_correlation_feasibility_summary,
    build_symbol_feasibility_summaries,
    classify_opportunity_today,
    classify_structural_suitability,
    expected_correlation_request_count,
    expected_opportunity_scan_request_count,
    expected_tradier_request_count,
    has_sufficient_observed_headroom,
    load_rate_limit_safety_config,
    usable_request_headroom,
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


class TestRateLimitSafetyConfigLoader:
    """The real config/universe_feasibility.yaml's `rate_limit_safety`
    section loads correctly, and a missing section/file fails closed."""

    def test_real_config_loads_with_conservative_defaults(self):
        cfg = load_rate_limit_safety_config(FEASIBILITY_CONFIG)
        assert cfg.batch_size == 3
        assert 0.0 <= cfg.reserved_headroom_pct < 1.0
        assert 0.0 <= cfg.correlation_reserved_headroom_pct < 1.0

    def test_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(RateLimitSafetyConfigError):
            load_rate_limit_safety_config(tmp_path / "does_not_exist.yaml")

    def test_missing_section_fails_closed(self, tmp_path):
        path = tmp_path / "universe_feasibility.yaml"
        path.write_text("tickers: [{ticker: SPY, sector: ETF}]\nstrategies: [CASH_SECURED_PUT]\n")
        with pytest.raises(RateLimitSafetyConfigError):
            load_rate_limit_safety_config(path)

    def test_env_override_applies(self, monkeypatch, tmp_path):
        path = tmp_path / "universe_feasibility.yaml"
        path.write_text(
            "tickers: [{ticker: SPY, sector: ETF}]\nstrategies: [CASH_SECURED_PUT]\n"
            "rate_limit_safety:\n"
            "  batch_size: 3\n"
            "  batch_size_env: TEST_BATCH_SIZE_OVERRIDE\n"
            "  reserved_headroom_pct: 0.2\n"
            "  correlation_reserved_headroom_pct: 0.3\n"
        )
        monkeypatch.setenv("TEST_BATCH_SIZE_OVERRIDE", "5")
        cfg = load_rate_limit_safety_config(path)
        assert cfg.batch_size == 5


class TestPerSymbolDiagnosticsDeterministic:
    """Item L: per-symbol diagnostics are complete and deterministic."""

    def test_symbol_summaries_reflect_construction_and_ranking_outcomes(self):
        scan_result, diagnostics, received = _run_scan(
            {"SPY": 605.0, "ILLIQUID": 50.0}, unconstructible=frozenset({"ILLIQUID"}),
        )
        summaries = build_symbol_feasibility_summaries(
            evaluated_tickers=("SPY", "ILLIQUID"), chain_received_tickers=received,
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
            evaluated_tickers=("SPY",), chain_received_tickers=received_1,
            diagnostics_by_ticker=diagnostics_1, scan_result=scan_result_1,
        )
        s2 = build_symbol_feasibility_summaries(
            evaluated_tickers=("SPY",), chain_received_tickers=received_2,
            diagnostics_by_ticker=diagnostics_2, scan_result=scan_result_2,
        )
        assert s1 == s2

    def test_ranked_candidates_clearing_hurdle_never_exceeds_ranked_candidates(self):
        scan_result, diagnostics, received = _run_scan({"SPY": 605.0})
        summaries = build_symbol_feasibility_summaries(
            evaluated_tickers=("SPY",), chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result, no_trade_hurdle=0.0,
        )
        s = summaries[0]
        assert s.ranked_candidates_clearing_hurdle <= s.ranked_candidates

    def test_only_evaluated_tickers_get_a_summary_never_a_skipped_one(self):
        scan_result, diagnostics, received = _run_scan({"SPY": 605.0})
        summaries = build_symbol_feasibility_summaries(
            evaluated_tickers=("SPY",), chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result,
        )
        assert {s.ticker for s in summaries} == {"SPY"}


class TestAggregateDiagnosticsReconcile:
    """Item M: aggregate totals reconcile with the per-symbol output,
    and evaluated/skipped bookkeeping is kept separate from a real
    market-data failure."""

    def test_aggregate_totals_match_sum_of_per_symbol_fields(self):
        scan_result, diagnostics, received = _run_scan({"SPY": 605.0, "QQQ": 500.0, "ILLIQUID": 50.0}, unconstructible=frozenset({"ILLIQUID"}))
        tickers = ("SPY", "QQQ", "ILLIQUID")
        summaries = build_symbol_feasibility_summaries(
            evaluated_tickers=tickers, chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result,
        )
        aggregate = build_aggregate_feasibility_report(
            symbols_requested=3, symbol_summaries=summaries,
            chain_received_tickers=received, scan_result=scan_result,
        )
        assert aggregate.total_construction_successes == sum(s.construction_successes for s in summaries)
        assert aggregate.total_quant_passed == sum(s.quant_passed for s in summaries)
        assert aggregate.total_risk_passed == sum(s.risk_passed for s in summaries)
        assert aggregate.total_ranked_candidates == sum(s.ranked_candidates for s in summaries)
        assert aggregate.symbols_requested == 3
        assert aggregate.symbols_evaluated == 3
        assert aggregate.symbols_skipped == 0
        assert aggregate.successful_chains == 3
        assert aggregate.failed_chains == 0

    def test_skipped_symbols_never_counted_as_failed_chains(self):
        scan_result, diagnostics, received = _run_scan({"SPY": 605.0})
        summaries = build_symbol_feasibility_summaries(
            evaluated_tickers=("SPY",), chain_received_tickers=received,
            diagnostics_by_ticker=diagnostics, scan_result=scan_result,
        )
        aggregate = build_aggregate_feasibility_report(
            symbols_requested=3, symbol_summaries=summaries, chain_received_tickers=received,
            scan_result=scan_result, skipped_tickers=("QQQ", "IWM"), skip_reason=SkipReason.RATE_LIMIT_HEADROOM,
        )
        assert aggregate.symbols_requested == 3
        assert aggregate.symbols_evaluated == 1
        assert aggregate.symbols_skipped == 2
        assert aggregate.skipped_tickers == ("QQQ", "IWM")
        assert aggregate.skip_reason == SkipReason.RATE_LIMIT_HEADROOM
        assert aggregate.failed_chains == 0, "a skipped symbol must never inflate failed_chains"
        assert aggregate.structural_suitability_counts.get(StructuralSuitability.NOT_EVALUATED.value) == 2


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


def _summary(**overrides):
    base = dict(
        ticker="TST", market_data_successful=True, chain_usable=True, contracts_seen=2,
        expirations_seen=1, expirations_eligible=1, expirations_rejected=0, strategy_attempts=1,
        construction_attempts=1, construction_successes=1, construction_rejections=0,
        generation_exceptions=0, quant_evaluations=1, quant_passed=1, quant_rejected=0,
        risk_evaluations=1, risk_passed=1, risk_rejected=0, ranked_candidates=1,
        ranked_candidates_clearing_hurdle=1,
        selected_candidate_proposal_id=None, dominant_rejection_reasons=(),
    )
    base.update(overrides)
    return SymbolFeasibilitySummary(**base)


class TestStructuralSuitabilityClassification:
    """Items N/G: deterministic STRONG/ACCEPTABLE/WEAK/UNSUITABLE rules
    based ONLY on chain/DTE/construction facts -- never on Quant/Risk/
    ranking/the no-trade hurdle, and never on today's market direction."""

    def test_market_data_failure_is_unsuitable(self):
        s = _summary(market_data_successful=False, chain_usable=False, expirations_eligible=0, construction_successes=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0, strategy_attempts=0)
        assert classify_structural_suitability(s) == StructuralSuitability.UNSUITABLE

    def test_zero_eligible_expirations_is_unsuitable(self):
        s = _summary(expirations_eligible=0, construction_successes=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_structural_suitability(s) == StructuralSuitability.UNSUITABLE

    def test_zero_construction_successes_is_weak(self):
        s = _summary(construction_successes=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_structural_suitability(s) == StructuralSuitability.WEAK

    def test_every_attempted_strategy_succeeding_is_strong(self):
        s = _summary(strategy_attempts=1, construction_successes=1, generation_exceptions=0)
        assert classify_structural_suitability(s) == StructuralSuitability.STRONG

    def test_not_every_attempted_strategy_succeeding_is_acceptable(self):
        s = _summary(strategy_attempts=3, construction_successes=1, generation_exceptions=0)
        assert classify_structural_suitability(s) == StructuralSuitability.ACCEPTABLE

    def test_a_generation_exception_prevents_strong_even_if_all_attempts_otherwise_succeeded(self):
        s = _summary(strategy_attempts=1, construction_successes=1, generation_exceptions=1)
        assert classify_structural_suitability(s) == StructuralSuitability.ACCEPTABLE

    def test_strong_never_requires_a_ranked_candidate(self):
        """The exact conflation the V1.5.14 acceptance audit found --
        STRONG must be reachable even with zero Quant/Risk/ranked
        candidates today."""
        s = _summary(strategy_attempts=1, construction_successes=1, generation_exceptions=0, quant_passed=0, risk_passed=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0, quant_evaluations=1, quant_rejected=1, risk_evaluations=0)
        assert classify_structural_suitability(s) == StructuralSuitability.STRONG

    def test_many_eligible_expirations_with_many_internal_rejections_still_strong(self):
        """construction_attempts (inflated by searching every eligible
        expiration for the single best candidate) must NEVER be the
        denominator -- only strategy_attempts (at most 3) is."""
        s = _summary(strategy_attempts=1, construction_attempts=6, construction_successes=1, construction_rejections=5, generation_exceptions=0)
        assert classify_structural_suitability(s) == StructuralSuitability.STRONG

    def test_classification_is_deterministic_never_based_on_direction_or_pnl(self):
        s = _summary()
        assert classify_structural_suitability(s) == classify_structural_suitability(s)

    def test_classify_never_returns_not_evaluated(self):
        """NOT_EVALUATED is assigned directly by the orchestration
        layer for a skipped ticker -- the pure classifier is never given
        the chance to return it."""
        for construction_successes in (0, 1):
            for market_data_successful in (True, False):
                s = _summary(construction_successes=construction_successes, market_data_successful=market_data_successful)
                assert classify_structural_suitability(s) != StructuralSuitability.NOT_EVALUATED


class TestOpportunityTodayClassification:
    """Item H: purely observational, never affects structural
    suitability, and tracks the real no-trade-hurdle comparison."""

    def test_zero_construction_successes_is_none(self):
        s = _summary(construction_successes=0, quant_passed=0, quant_evaluations=0, risk_passed=0, risk_evaluations=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_opportunity_today(s) == OpportunityToday.NONE

    def test_constructed_but_no_quant_pass_is_constructed(self):
        s = _summary(construction_successes=1, quant_passed=0, quant_evaluations=1, quant_rejected=1, risk_passed=0, risk_evaluations=0, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_opportunity_today(s) == OpportunityToday.CONSTRUCTED

    def test_quant_pass_but_no_risk_pass_is_quant_pass(self):
        s = _summary(construction_successes=1, quant_passed=1, risk_passed=0, risk_evaluations=1, risk_rejected=1, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_opportunity_today(s) == OpportunityToday.QUANT_PASS

    def test_risk_pass_but_not_ranked_is_risk_pass(self):
        s = _summary(construction_successes=1, quant_passed=1, risk_passed=1, ranked_candidates=0, ranked_candidates_clearing_hurdle=0)
        assert classify_opportunity_today(s) == OpportunityToday.RISK_PASS

    def test_ranked_but_not_clearing_hurdle_is_ranked(self):
        s = _summary(construction_successes=1, quant_passed=1, risk_passed=1, ranked_candidates=1, ranked_candidates_clearing_hurdle=0)
        assert classify_opportunity_today(s) == OpportunityToday.RANKED

    def test_clearing_hurdle_is_cleared_hurdle(self):
        s = _summary(construction_successes=1, quant_passed=1, risk_passed=1, ranked_candidates=1, ranked_candidates_clearing_hurdle=1)
        assert classify_opportunity_today(s) == OpportunityToday.CLEARED_HURDLE

    def test_opportunity_today_never_affects_structural_suitability(self):
        strong_but_no_opportunity = _summary(
            strategy_attempts=1, construction_successes=1, generation_exceptions=0,
            quant_passed=0, quant_evaluations=1, quant_rejected=1, risk_passed=0, risk_evaluations=0,
            ranked_candidates=0, ranked_candidates_clearing_hurdle=0,
        )
        assert classify_structural_suitability(strong_but_no_opportunity) == StructuralSuitability.STRONG
        assert classify_opportunity_today(strong_but_no_opportunity) == OpportunityToday.CONSTRUCTED


class TestRateLimitRequestBudget:
    """Item P (deterministic half) -- V1.5.14 acceptance correction:
    the corrected formula accounts for get_option_chain_for_expiration's
    own internal quote re-fetch (2 requests per selected expiration, not
    1), matching an instrumented real-provider run exactly (see the
    audit's own empirical verification)."""

    def test_opportunity_scan_phase_twelve_symbols_six_expirations(self):
        assert expected_opportunity_scan_request_count(num_symbols=12, max_expirations=6) == 12 * (2 + 2 * 6)

    def test_correlation_phase_twelve_symbols(self):
        assert expected_correlation_request_count(num_symbols=12) == 12

    def test_complete_twelve_symbol_estimate_is_180(self):
        assert expected_tradier_request_count(num_symbols=12, max_expirations=6) == 180

    def test_one_symbol_six_expirations_is_fifteen(self):
        assert expected_tradier_request_count(num_symbols=1, max_expirations=6) == 15

    def test_zero_symbols_is_zero_requests(self):
        assert expected_tradier_request_count(num_symbols=0, max_expirations=6) == 0

    def test_negative_num_symbols_rejected(self):
        with pytest.raises(ValueError):
            expected_tradier_request_count(num_symbols=-1, max_expirations=6)

    def test_negative_max_expirations_rejected(self):
        with pytest.raises(ValueError):
            expected_tradier_request_count(num_symbols=12, max_expirations=-1)

    def test_negative_num_symbols_rejected_in_correlation_phase(self):
        with pytest.raises(ValueError):
            expected_correlation_request_count(num_symbols=-1)


class TestObservedHeadroom:
    """V1.5.14 acceptance correction: headroom decisions use ONLY the
    provider's own observed RateLimitState -- never a hardcoded plan
    allowance such as 120/minute."""

    def _state(self, *, allowed=100, used=0, available=100):
        return RateLimitState(allowed=allowed, used=used, available=available, reset_at=None, observed_at=NOW)

    def test_unknown_state_always_proceeds(self):
        assert has_sufficient_observed_headroom(None, projected_requests=1_000_000, reserved_headroom_pct=0.2) is True

    def test_unknown_state_has_no_numeric_headroom(self):
        assert usable_request_headroom(None, reserved_headroom_pct=0.2) is None

    def test_sufficient_headroom_proceeds(self):
        state = self._state(allowed=100, available=90)
        # reserve 20% of 100 = 20 -- usable = 90 - 20 = 70
        assert has_sufficient_observed_headroom(state, projected_requests=70, reserved_headroom_pct=0.2) is True
        assert usable_request_headroom(state, reserved_headroom_pct=0.2) == 70

    def test_insufficient_headroom_stops(self):
        state = self._state(allowed=100, available=90)
        assert has_sufficient_observed_headroom(state, projected_requests=71, reserved_headroom_pct=0.2) is False

    def test_headroom_never_goes_negative(self):
        state = self._state(allowed=100, available=5)
        # reserve 20% of 100 = 20, available=5 -- 5-20 = -15, floored at 0
        assert usable_request_headroom(state, reserved_headroom_pct=0.2) == 0
        assert has_sufficient_observed_headroom(state, projected_requests=1, reserved_headroom_pct=0.2) is False

    def test_available_at_or_below_zero_never_proceeds(self):
        state = self._state(allowed=100, available=0)
        assert has_sufficient_observed_headroom(state, projected_requests=1, reserved_headroom_pct=0.0) is False

    def test_reserved_headroom_pct_out_of_range_rejected(self):
        state = self._state()
        with pytest.raises(ValueError):
            has_sufficient_observed_headroom(state, projected_requests=1, reserved_headroom_pct=1.0)
        with pytest.raises(ValueError):
            usable_request_headroom(state, reserved_headroom_pct=-0.1)

    def test_never_reads_a_hardcoded_plan_allowance(self):
        """Only `allowed`/`available` (both header-derived) are read --
        a state whose `allowed` is far from any conventional Tradier
        plan figure (e.g. 7) still produces a correct, proportional
        decision, proving no number like 120 is baked in anywhere."""
        state = self._state(allowed=7, available=7)
        # reserve 20% of 7 = 1.4 -- usable = 7 - 1.4 = 5.6 -> floor 5
        assert usable_request_headroom(state, reserved_headroom_pct=0.2) == 5


class TestCorrelationFeasibilitySummary:
    """Item H (status half): sufficient/insufficient/skipped-budget are
    reported separately, and skipped-budget is decided entirely by the
    caller (never inferred from an absence in price_history)."""

    def test_sufficient_insufficient_and_skipped_are_disjoint(self):
        summary = build_correlation_feasibility_summary(
            correlation_attempted_tickers=("SPY", "QQQ"), correlation_skipped_tickers=("IWM",),
            price_history={"SPY": [1.0, 2.0]},
        )
        assert summary.symbols_with_sufficient_history == ("SPY",)
        assert summary.symbols_with_insufficient_history == ("QQQ",)
        assert summary.symbols_skipped_budget == ("IWM",)

    def test_high_correlation_pairs_pass_through_unchanged(self):
        pairs = (("SPY", "QQQ", 0.95),)
        summary = build_correlation_feasibility_summary(
            correlation_attempted_tickers=("SPY", "QQQ"), correlation_skipped_tickers=(),
            price_history={"SPY": [1.0], "QQQ": [1.0]}, high_correlation_pairs=pairs,
        )
        assert summary.high_correlation_pairs == pairs

    def test_a_skipped_ticker_is_never_double_counted_as_insufficient(self):
        summary = build_correlation_feasibility_summary(
            correlation_attempted_tickers=(), correlation_skipped_tickers=("SPY", "QQQ"), price_history={},
        )
        assert summary.symbols_with_insufficient_history == ()
        assert summary.symbols_skipped_budget == ("QQQ", "SPY")


def test_correlation_symbol_status_has_the_three_required_values():
    assert {s.value for s in CorrelationSymbolStatus} == {"SUFFICIENT_HISTORY", "INSUFFICIENT_HISTORY", "SKIPPED_BUDGET"}


def test_skip_reason_has_rate_limit_headroom():
    assert SkipReason.RATE_LIMIT_HEADROOM.value == "RATE_LIMIT_HEADROOM"
