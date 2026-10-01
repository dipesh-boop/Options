"""PAPER_TRADING_V1.5.5, Step 4: unit tests for
`src.workflows.candidate_funnel.build_candidate_funnel` -- funnel-count
accuracy, rejection-reason aggregation, bounded cardinality, deterministic
zero-candidate summaries, and `ControlCycleRecord` backward compatibility.

Real-pipeline accuracy is proven by actually running
`scan_and_rank_opportunities` (never hand-faked counts); bounding/
determinism, where a realistic scenario would need dozens of tickers to
exercise, is proven directly against `build_candidate_funnel`'s own
synthetic inputs -- both are legitimate, and neither substitutes for the
behavioral-equivalence proof in `test_candidate_funnel_equivalence.py`,
which this file does not duplicate.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.opportunity_scan import OpportunityScanResult, scan_and_rank_opportunities
from src.portfolio.persistence import InMemoryControlLoopStore
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.workflows.candidate_funnel import CandidateFunnel, build_candidate_funnel
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry
from src.workflows.funnel_diagnostics import FunnelDiagnostics

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()
BROKER_CAPS = load_broker_capabilities("internal_paper")
QUANT_FILTER = QuantFilterConfig(min_dte=20, max_dte=45)


def _spy_chain(*, oi=1000, vol=500) -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1_000_000, timestamp=NOW, source="tradier")
    contracts = [
        OptionContract(
            underlying="SPY", option_symbol=f"SPY{EXP.isoformat()}P{int(strike*1000):08d}", expiration=EXP, strike=strike,
            right=OptionRight.PUT, bid=3.0, ask=3.2, last=3.1, volume=vol, open_interest=oi,
            delta=delta, iv=0.18, underlying_price=455.0, timestamp=NOW, source="tradier",
        )
        for strike, delta in [(450.0, -0.20), (445.0, -0.12), (440.0, -0.08)]
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=NOW, source="tradier")


def _portfolio(nav: float) -> Portfolio:
    return Portfolio(as_of=NOW, nav=nav, cash=nav, peak_equity=nav)


def _scan_with_diagnostics(universe, chains, strategies, portfolio, **kwargs):
    diagnostics_by_ticker = {e.ticker: FunnelDiagnostics(ticker=e.ticker) for e in universe}
    result = scan_and_rank_opportunities(
        universe, chains, strategies, QUANT_FILTER, LIMITS, portfolio, "normal", BROKER_CAPS,
        now=NOW, diagnostics_by_ticker=diagnostics_by_ticker, **kwargs,
    )
    return result, diagnostics_by_ticker


class TestBuildCandidateFunnelAgainstRealPipeline:
    def test_zero_candidate_funnel_counts_are_accurate(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain(oi=1, vol=1)}  # fails liquidity on every contract
        result, diag = _scan_with_diagnostics(universe, chains, [StrategyType.CASH_SECURED_PUT], _portfolio(5_000_000.0))
        funnel = build_candidate_funnel(
            cycle_id="cyc-1", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker=diag, scan_result=result, candidates_persisted=0,
        )
        assert funnel.symbols_requested == 1
        assert funnel.symbols_market_data_successful == 1
        assert funnel.symbols_market_data_failed == 0
        assert funnel.option_chains_received == 1
        assert funnel.option_chains_quality_passed == 1
        assert funnel.option_chains_quality_failed == 0
        assert funnel.contracts_seen == 3
        assert funnel.strategy_attempts == 1
        assert funnel.construction_attempts >= 1
        assert funnel.construction_successes == 0
        assert funnel.construction_rejections >= 1
        assert funnel.quant_evaluations == 0  # no candidate was ever built, so quant/risk never ran
        assert funnel.risk_evaluations == 0
        assert funnel.candidates_generated == 0
        assert funnel.candidates_selected == 0
        assert funnel.candidates_persisted_for_review == 0
        assert funnel.zero_candidate_summary is not None
        assert "symbols_scanned=1" in funnel.zero_candidate_summary
        assert any(r.stage == "construction" for r in funnel.rejection_reasons)

    def test_covered_call_no_shares_is_visible_as_a_strategy_prerequisite_rejection(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        result, diag = _scan_with_diagnostics(universe, chains, [StrategyType.COVERED_CALL], _portfolio(5_000_000.0))
        funnel = build_candidate_funnel(
            cycle_id="cyc-2", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker=diag, scan_result=result, candidates_persisted=0,
        )
        assert funnel.strategy_ineligible == 1
        assert any(
            r.stage == "strategy_prerequisite" and r.reason == "COVERED_CALL_NO_SHARES" and r.count == 1
            for r in funnel.rejection_reasons
        )
        strat = next(s for s in funnel.by_strategy if s.strategy == "COVERED_CALL")
        assert strat.ineligible == 1
        assert strat.attempts == 0

    def test_risk_rejection_is_visible_with_the_risk_engines_own_reason_codes(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        result, diag = _scan_with_diagnostics(universe, chains, [StrategyType.CASH_SECURED_PUT], _portfolio(100_000.0))
        funnel = build_candidate_funnel(
            cycle_id="cyc-3", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker=diag, scan_result=result, candidates_persisted=0,
        )
        assert funnel.quant_evaluations == 1
        assert funnel.quant_passed == 1
        assert funnel.risk_evaluations == 1
        assert funnel.risk_rejected == 1
        assert funnel.risk_passed == 0
        assert any(r.stage == "risk" for r in funnel.rejection_reasons)
        strat = next(s for s in funnel.by_strategy if s.strategy == "CASH_SECURED_PUT")
        assert strat.risk_rejected == 1

    def test_candidates_persisted_for_review_reflects_the_caller_supplied_value_not_a_guess(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        result, diag = _scan_with_diagnostics(
            universe, chains, [StrategyType.CASH_SECURED_PUT], _portfolio(5_000_000.0), no_trade_hurdle=-1_000.0,
        )
        assert result.best is not None  # sanity: this scenario really does select a candidate
        funnel = build_candidate_funnel(
            cycle_id="cyc-4", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker=diag, scan_result=result, candidates_persisted=1,
        )
        assert funnel.candidates_selected == 1
        assert funnel.candidates_persisted_for_review == 1
        assert funnel.zero_candidate_summary is None


class TestBuildCandidateFunnelBoundingAndDeterminism:
    """Direct, synthetic-input tests -- `build_candidate_funnel` is pure
    and takes plain data, so cardinality/bounding/determinism can be
    proven without a 50-ticker universe or a real option chain."""

    def _empty_scan_result(self) -> OpportunityScanResult:
        return OpportunityScanResult(scanned=(), best=None, no_trade_reason="no candidates this synthetic cycle")

    def test_build_candidate_funnel_is_deterministic_for_identical_inputs(self):
        tickers = tuple(f"T{i:02d}" for i in range(5))
        diag = {}
        for i, t in enumerate(tickers):
            d = FunnelDiagnostics(ticker=t)
            d.record_chain(contracts_seen=10 + i, stale=False)
            d.record_expirations(seen=3, eligible=2)
            d.record_strategy_attempt("CASH_SECURED_PUT")
            d.record_construction("CASH_SECURED_PUT", success=False, reason=f"REASON_{i % 3}")
            diag[t] = d
        kwargs = dict(
            cycle_id="cyc-det", generated_at=NOW, universe_tickers=tickers, chain_received_tickers=frozenset(tickers),
            diagnostics_by_ticker=diag, scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        a = build_candidate_funnel(**kwargs)
        b = build_candidate_funnel(**kwargs)
        assert a == b

    def test_rejection_reasons_are_bounded_and_sorted_by_count_descending(self):
        tickers = tuple(f"T{i:02d}" for i in range(40))
        diag = {}
        for i, t in enumerate(tickers):
            d = FunnelDiagnostics(ticker=t)
            d.record_chain(contracts_seen=1, stale=False)
            d.record_strategy_attempt("CASH_SECURED_PUT")
            # 40 distinct reasons -- more than _MAX_REJECTION_REASONS (25)
            d.record_construction("CASH_SECURED_PUT", success=False, reason=f"REASON_{i}")
            diag[t] = d
        funnel = build_candidate_funnel(
            cycle_id="cyc-bound", generated_at=NOW, universe_tickers=tickers, chain_received_tickers=frozenset(tickers),
            diagnostics_by_ticker=diag, scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert len(funnel.rejection_reasons) <= 25
        counts = [r.count for r in funnel.rejection_reasons]
        assert counts == sorted(counts, reverse=True)

    def test_top_bottlenecks_bounded_to_five_and_reflects_the_most_common_reasons(self):
        tickers = ("A", "B", "C")
        diag = {}
        # "OFTEN" rejected 3x, "RARE" rejected once
        for i, t in enumerate(tickers):
            d = FunnelDiagnostics(ticker=t)
            d.record_chain(contracts_seen=1, stale=False)
            d.record_strategy_attempt("CASH_SECURED_PUT")
            d.record_construction("CASH_SECURED_PUT", success=False, reason="OFTEN" if i < 3 else "RARE")
            diag[t] = d
        funnel = build_candidate_funnel(
            cycle_id="cyc-bottleneck", generated_at=NOW, universe_tickers=tickers, chain_received_tickers=frozenset(tickers),
            diagnostics_by_ticker=diag, scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert len(funnel.top_bottlenecks) <= 5
        assert "OFTEN" in funnel.top_bottlenecks[0]

    def test_by_symbol_and_by_strategy_summaries_are_bounded(self):
        tickers = tuple(f"T{i:03d}" for i in range(60))  # more than _MAX_SYMBOL_SUMMARIES (50)
        diag = {t: FunnelDiagnostics(ticker=t) for t in tickers}
        funnel = build_candidate_funnel(
            cycle_id="cyc-symbols", generated_at=NOW, universe_tickers=tickers, chain_received_tickers=frozenset(tickers),
            diagnostics_by_ticker=diag, scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert len(funnel.by_symbol) <= 50

    def test_missing_diagnostics_for_a_universe_ticker_is_recorded_not_silently_dropped(self):
        # A ticker in the universe with no FunnelDiagnostics object at all
        # (e.g. its chain fetch failed before diagnostics collection could
        # even start) must still appear in by_symbol, honestly marked
        # unusable -- never fabricated as if it were scanned.
        funnel = build_candidate_funnel(
            cycle_id="cyc-missing", generated_at=NOW, universe_tickers=("SPY", "QQQ"), chain_received_tickers=frozenset(),
            diagnostics_by_ticker={}, scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert funnel.symbols_market_data_successful == 0
        assert funnel.symbols_market_data_failed == 2
        tickers_seen = {s.ticker for s in funnel.by_symbol}
        assert tickers_seen == {"SPY", "QQQ"}
        assert all(not s.market_data_successful and not s.chain_usable for s in funnel.by_symbol)

    def test_no_secret_or_token_ever_appears_in_a_built_funnel(self):
        diag = {"SPY": FunnelDiagnostics(ticker="SPY")}
        diag["SPY"].record_chain(contracts_seen=3, stale=False)
        diag["SPY"].record_strategy_attempt("CASH_SECURED_PUT")
        diag["SPY"].record_construction("CASH_SECURED_PUT", success=False, reason="LIQUIDITY_OPEN_INTEREST")
        funnel = build_candidate_funnel(
            cycle_id="cyc-no-forbidden-strings-test", generated_at=NOW, universe_tickers=("SPY",),
            chain_received_tickers=frozenset({"SPY"}), diagnostics_by_ticker=diag,
            scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        serialized = funnel.model_dump_json().lower()
        for forbidden in ("bearer", "token", "authorization", "api_key", "apikey", "secret", "password"):
            assert forbidden not in serialized


class TestControlCycleRecordBackwardCompatibility:
    def test_old_record_json_without_candidate_funnel_field_still_deserializes(self):
        old_json = json.dumps(
            {
                "cycle_id": "cyc-old", "started_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
                "market_open": True, "provider": "tradier", "provider_health_status": "healthy",
                "symbols_requested": ["SPY"], "symbols_successful": ["SPY"], "symbols_failed": [],
                "positions_evaluated": 0, "lifecycle_triggers": 0, "risk_events": 0, "recommendations_created": 0,
                "opportunities_scanned": 1, "candidates_generated": 0, "candidates_rejected": 0,
                "requests_used": None, "rate_limit_available": None,
                "errors": [], "degraded_mode": False, "halt_state": False,
                # deliberately no "experiment_version_id" and no "candidate_funnel" key --
                # a genuine pre-V1.5.0 record.
            }
        )
        record = ControlCycleRecord.model_validate_json(old_json)
        assert record.candidate_funnel is None
        assert record.experiment_version_id is None

    def test_control_loop_store_round_trips_a_record_with_no_candidate_funnel(self, tmp_path):
        store = InMemoryControlLoopStore()
        record = ControlCycleRecord(
            cycle_id="cyc-roundtrip", started_at=NOW, completed_at=NOW, market_open=True,
            provider="tradier", provider_health_status="healthy",
        )
        assert record.candidate_funnel is None
        store.save_cycle_record(record)
        loaded = store.get_cycle_record("cyc-roundtrip")
        assert loaded is not None
        assert loaded.candidate_funnel is None

    def test_control_loop_store_round_trips_a_record_carrying_a_candidate_funnel(self):
        store = InMemoryControlLoopStore()
        funnel = build_candidate_funnel(
            cycle_id="cyc-with-funnel", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker={"SPY": FunnelDiagnostics(ticker="SPY")},
            scan_result=OpportunityScanResult(scanned=(), best=None, no_trade_reason="none"),
            candidates_persisted=0,
        )
        record = ControlCycleRecord(
            cycle_id="cyc-with-funnel", started_at=NOW, completed_at=NOW, market_open=True,
            provider="tradier", provider_health_status="healthy", candidate_funnel=funnel,
        )
        store.save_cycle_record(record)
        loaded = store.get_cycle_record("cyc-with-funnel")
        assert loaded is not None
        assert loaded.candidate_funnel == funnel


class TestExpirationDteOutOfRangeRejectionReason:
    """PAPER_TRADING_V1.5.6: closes the V1.5.5 observability gap the
    2026-10-01 production incident exposed -- when a provider's chain
    carries expirations outside the candidate engine's own
    `[min_dte, max_dte]` window, `expirations_rejected` was always
    visible on `CandidateFunnel` itself, but NO entry ever appeared in
    `rejection_reasons`/`top_bottlenecks`, so an unrelated, coincidental
    construction-rejection reason (e.g. `COVERED_CALL_NO_SHARES`) could
    misleadingly appear to be the dominant bottleneck when expiration
    eligibility had actually eliminated every candidate before
    construction was ever attempted. Derived purely from
    `expirations_seen - expirations_eligible` -- no new `FunnelDiagnostics`
    field, no change to `expirations_seen`/`expirations_eligible`
    themselves, no change to trading behavior."""

    def _empty_scan_result(self) -> OpportunityScanResult:
        return OpportunityScanResult(scanned=(), best=None, no_trade_reason="no candidates this synthetic cycle")

    def _out_of_window_chain(self) -> OptionChain:
        # 3 DTE from NOW (2026-09-22) -- well below QUANT_FILTER's min_dte=20.
        exp = date(2026, 9, 25)
        underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1_000_000, timestamp=NOW, source="tradier")
        contracts = [
            OptionContract(
                underlying="SPY", option_symbol=f"SPY{exp.isoformat()}P{int(strike*1000):08d}", expiration=exp, strike=strike,
                right=OptionRight.PUT, bid=3.0, ask=3.2, last=3.1, volume=500, open_interest=1000,
                delta=delta, iv=0.18, underlying_price=455.0, timestamp=NOW, source="tradier",
            )
            for strike, delta in [(450.0, -0.20), (445.0, -0.12)]
        ]
        return OptionChain(underlying=underlying, contracts=contracts, timestamp=NOW, source="tradier")

    def test_reproduces_the_2026_10_01_incident_shape_against_the_real_pipeline(self):
        # expirations_seen > 0, expirations_eligible == 0 -- exactly the
        # production defect's funnel shape (expirations_eligible=0,
        # expirations_rejected=12, candidates_generated=0).
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": self._out_of_window_chain()}
        result, diag = _scan_with_diagnostics(universe, chains, [StrategyType.CASH_SECURED_PUT], _portfolio(5_000_000.0))
        funnel = build_candidate_funnel(
            cycle_id="cyc-dte-oor", generated_at=NOW, universe_tickers=("SPY",), chain_received_tickers=frozenset({"SPY"}),
            diagnostics_by_ticker=diag, scan_result=result, candidates_persisted=0,
        )
        assert funnel.expirations_seen == 1
        assert funnel.expirations_eligible == 0
        assert funnel.expirations_rejected == 1
        assert funnel.candidates_generated == 0
        assert any(
            r.stage == "expiration" and r.reason == "EXPIRATION_DTE_OUT_OF_RANGE" and r.count == 1
            for r in funnel.rejection_reasons
        )
        assert any("EXPIRATION_DTE_OUT_OF_RANGE" in b for b in funnel.top_bottlenecks)

    def test_rejected_expirations_count_matches_seen_minus_eligible_across_multiple_tickers(self):
        diag = {}
        for ticker, seen, eligible in [("AAA", 5, 1), ("BBB", 3, 3), ("CCC", 2, 0)]:
            d = FunnelDiagnostics(ticker=ticker)
            d.record_expirations(seen=seen, eligible=eligible)
            diag[ticker] = d
        funnel = build_candidate_funnel(
            cycle_id="cyc-dte-multi", generated_at=NOW, universe_tickers=("AAA", "BBB", "CCC"),
            chain_received_tickers=frozenset({"AAA", "BBB", "CCC"}), diagnostics_by_ticker=diag,
            scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        # (5-1) + (3-3) + (2-0) = 4 + 0 + 2 = 6
        total_rejected = next(
            (r.count for r in funnel.rejection_reasons if r.stage == "expiration" and r.reason == "EXPIRATION_DTE_OUT_OF_RANGE"),
            0,
        )
        assert total_rejected == 6
        assert funnel.expirations_seen == 10
        assert funnel.expirations_eligible == 4
        assert funnel.expirations_rejected == 6

    def test_no_spurious_reason_when_every_seen_expiration_is_eligible(self):
        diag = {"SPY": FunnelDiagnostics(ticker="SPY")}
        diag["SPY"].record_expirations(seen=4, eligible=4)
        funnel = build_candidate_funnel(
            cycle_id="cyc-dte-all-eligible", generated_at=NOW, universe_tickers=("SPY",),
            chain_received_tickers=frozenset({"SPY"}), diagnostics_by_ticker=diag,
            scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert funnel.expirations_rejected == 0
        assert not any(r.stage == "expiration" and r.reason == "EXPIRATION_DTE_OUT_OF_RANGE" for r in funnel.rejection_reasons)

    def test_no_spurious_reason_when_no_expirations_were_ever_seen(self):
        # A ticker whose chain fetch failed entirely (expirations_seen=0,
        # expirations_eligible=0) must never be counted as a DTE rejection
        # -- rejected_expirations would be 0 - 0 = 0, correctly a no-op.
        diag = {"SPY": FunnelDiagnostics(ticker="SPY")}
        funnel = build_candidate_funnel(
            cycle_id="cyc-dte-never-seen", generated_at=NOW, universe_tickers=("SPY",),
            chain_received_tickers=frozenset(), diagnostics_by_ticker=diag,
            scan_result=self._empty_scan_result(), candidates_persisted=0,
        )
        assert funnel.expirations_seen == 0
        assert funnel.expirations_eligible == 0
        assert not any(r.stage == "expiration" and r.reason == "EXPIRATION_DTE_OUT_OF_RANGE" for r in funnel.rejection_reasons)

    def test_covered_call_no_shares_no_longer_appears_as_the_sole_or_dominant_bottleneck(self):
        # The exact V1.5.5 observability gap this closes: before this fix,
        # an expiration-eliminated cycle's rejection_reasons contained
        # ONLY the coincidental COVERED_CALL_NO_SHARES strategy-prerequisite
        # rejection (itself real, but not the actual bottleneck) -- now
        # EXPIRATION_DTE_OUT_OF_RANGE must also appear, and with a higher
        # count whenever more expirations were rejected than strategies
        # were attempted.
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": self._out_of_window_chain()}
        result, diag = _scan_with_diagnostics(universe, chains, [StrategyType.COVERED_CALL], _portfolio(5_000_000.0))
        funnel = build_candidate_funnel(
            cycle_id="cyc-dte-covered-call", generated_at=NOW, universe_tickers=("SPY",),
            chain_received_tickers=frozenset({"SPY"}), diagnostics_by_ticker=diag,
            scan_result=result, candidates_persisted=0,
        )
        reasons_by_key = {(r.stage, r.reason): r.count for r in funnel.rejection_reasons}
        assert ("expiration", "EXPIRATION_DTE_OUT_OF_RANGE") in reasons_by_key
        assert reasons_by_key[("expiration", "EXPIRATION_DTE_OUT_OF_RANGE")] == 1
