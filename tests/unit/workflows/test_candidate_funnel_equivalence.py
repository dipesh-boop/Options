"""PAPER_TRADING_V1.5.5, Step 4: behavioral-equivalence (shadow/
differential) tests for candidate-funnel observability.

**What this file proves.** For each of the 10 named scenarios below,
`src.portfolio.opportunity_scan.scan_and_rank_opportunities` is run
TWICE against byte-identical inputs -- once with `diagnostics_by_ticker=
None` (V1.5.4 behavior, diagnostics collection entirely absent) and once
with a populated `diagnostics_by_ticker` (V1.5.5, diagnostics collection
enabled) -- and the two `OpportunityScanResult`s must be, and are,
identical in every field that could affect a trading decision: which
candidates were generated, their Quant/Risk results, ranking, the
selected `best` candidate (or CASH/NO_TRADE), and the reason given.
Only `src.workflows.candidate_funnel.FunnelDiagnostics`'s own internal
bookkeeping is allowed to differ between the two runs -- see
`_assert_scan_equivalent` below, which asserts exact equality on
`OpportunityScanResult` (a diagnostics-free type) and therefore proves
this structurally, not just for the specific fields spot-checked.

This is the proof `src.workflows.candidate_funnel`'s own module
docstring and `src.portfolio.opportunity_scan.scan_and_rank_opportunities`'s
own docstring both point to.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio import opportunity_scan as opportunity_scan_module
from src.portfolio.opportunity_scan import OpportunityScanResult, scan_and_rank_opportunities
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg, UnderlyingHolding
from src.risk.reason_codes import RiskDecision
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry
from src.workflows.funnel_diagnostics import FunnelDiagnostics

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)  # 31 days out from NOW -- inside the default [20, 45] DTE window
LIMITS = get_default_limits()
BROKER_CAPS = load_broker_capabilities("internal_paper")
QUANT_FILTER = QuantFilterConfig(min_dte=20, max_dte=45)


def _put_contracts(*, timestamp=NOW, oi=1000, vol=500) -> list[OptionContract]:
    # short_delta_low/high default to (0.15, 0.30) -- only the 450 strike
    # (delta -0.20) is ever in the target-delta window; 445/440 exist so
    # `_closest_by_target_delta` has out-of-window neighbors to skip over,
    # exactly like `tests/unit/portfolio/test_opportunity_scan.py`'s own
    # `_spy_chain` fixture.
    return [
        OptionContract(
            underlying="SPY", option_symbol=f"SPY{EXP.isoformat()}P{int(strike*1000):08d}", expiration=EXP, strike=strike,
            right=OptionRight.PUT, bid=3.0, ask=3.2, last=3.1, volume=vol, open_interest=oi,
            delta=delta, iv=0.18, underlying_price=455.0, timestamp=timestamp, source="tradier",
        )
        for strike, delta in [(450.0, -0.20), (445.0, -0.12), (440.0, -0.08)]
    ]


def _spy_chain(*, timestamp=NOW, oi=1000, vol=500) -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1_000_000, timestamp=timestamp, source="tradier")
    return OptionChain(underlying=underlying, contracts=_put_contracts(timestamp=timestamp, oi=oi, vol=vol), timestamp=timestamp, source="tradier")


def _portfolio(nav: float, *, positions=(), holdings: dict[str, UnderlyingHolding] | None = None) -> Portfolio:
    return Portfolio(
        as_of=NOW, nav=nav, cash=nav, peak_equity=nav, positions=list(positions),
        underlying_holdings=holdings or {},
    )


def _existing_position(position_id="p1", ticker="SPY") -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=EXP,
        legs=[
            PortfolioPositionLeg(right="P", side="sell", strike=430.0, entry_price=6.0),
            PortfolioPositionLeg(right="P", side="buy", strike=420.0, entry_price=3.0),
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0, opened_at=NOW - timedelta(days=5),
    )


def _run(
    universe, chains, strategies, quant_filter, limits, portfolio, *, market_regime="normal",
    broker_capabilities=BROKER_CAPS, now=NOW, no_trade_hurdle=0.0, with_diagnostics: bool,
) -> OpportunityScanResult:
    """Runs the real, unmodified scan once. `with_diagnostics=False`
    reproduces exact V1.5.4 call shape (the `diagnostics_by_ticker`
    kwarg omitted entirely is not tested here on purpose -- passing an
    explicit `None` and omitting the kwarg are the same default, and the
    signature-level proof of that is `test_candidate_generation.py`'s
    own diagnostics-optional tests); `with_diagnostics=True` is exactly
    what `src.portfolio.orchestrator._run_opportunity_scan_stage` builds
    when `OpportunityScanConfig.collect_candidate_funnel=True`."""
    diagnostics_by_ticker = (
        {e.ticker: FunnelDiagnostics(ticker=e.ticker) for e in universe} if with_diagnostics else None
    )
    return scan_and_rank_opportunities(
        universe, chains, strategies, quant_filter, limits, portfolio, market_regime, broker_capabilities,
        now=now, no_trade_hurdle=no_trade_hurdle, diagnostics_by_ticker=diagnostics_by_ticker,
    )


def _assert_scan_equivalent(a: OpportunityScanResult, b: OpportunityScanResult) -> None:
    """The structural proof: `OpportunityScanResult`/`ScannedCandidate`
    carry no diagnostics-collection field themselves (that lives
    entirely in the separate `FunnelDiagnostics` objects the caller
    holds), so exact dataclass equality here is exactly "every trading
    decision was identical" -- candidate identity, Quant result, Risk
    decision and reason codes, post-trade exposure, ranking (`scanned`
    is built and ordered identically), `best`, and `no_trade_reason`."""
    assert a.scanned == b.scanned, "diagnostics collection changed the scanned candidate set/order/content"
    assert a.candidates_generated == b.candidates_generated
    assert a.candidates_rejected == b.candidates_rejected
    assert (a.best is None) == (b.best is None), "diagnostics collection changed whether a candidate was selected"
    if a.best is not None:
        assert a.best.candidate.proposal.proposal_id == b.best.candidate.proposal.proposal_id
        assert a.best == b.best
    assert a.no_trade_reason == b.no_trade_reason


class TestCandidateFunnelBehavioralEquivalence:
    # ---------------------------------------------------- 1. zero-candidate day
    def test_scenario_1_zero_candidate_day(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        without = _run(universe, {}, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(100_000.0), with_diagnostics=False)
        with_ = _run(universe, {}, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(100_000.0), with_diagnostics=True)
        assert without.candidates_generated == 0 and without.best is None
        _assert_scan_equivalent(without, with_)

    # --------------------------------------------- 2. rejected by data quality
    def test_scenario_2_rejected_by_data_quality_stale_chain(self):
        stale_ts = NOW - timedelta(hours=2)
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain(timestamp=stale_ts)}
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=True)
        assert without.candidates_generated == 0, "a stale chain must yield zero candidates for this scenario to be meaningful"
        _assert_scan_equivalent(without, with_)

    # ----------------------------------------------- 3. rejected by liquidity
    def test_scenario_3_rejected_by_liquidity(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        # open_interest=50 < min_open_interest=100 (config/risk_limits.yaml)
        # on every contract, including the in-target-delta 450 strike --
        # `_closest_by_target_delta` still finds it, but the liquidity gate
        # then rejects it, yielding zero candidates.
        chains = {"SPY": _spy_chain(oi=50, vol=5)}
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=True)
        assert without.candidates_generated == 0, "an illiquid chain must yield zero candidates for this scenario to be meaningful"
        _assert_scan_equivalent(without, with_)

    # --------------------------------------------------- 4. rejected by Quant
    def test_scenario_4_rejected_by_quant(self, monkeypatch):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}

        def _raising_quant_stage(*args, **kwargs):
            raise ValueError("synthetic Quant-stage failure for scenario 4")

        monkeypatch.setattr(opportunity_scan_module, "default_quant_stage", _raising_quant_stage)
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=True)
        assert without.candidates_generated == 1
        assert without.scanned[0].quantitative_analysis is None
        assert without.scanned[0].risk_decision == RiskDecision.REJECT
        _assert_scan_equivalent(without, with_)

    # ---------------------------------------------------- 5. rejected by Risk
    def test_scenario_5_rejected_by_risk_undersized_portfolio(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(100_000.0), with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(100_000.0), with_diagnostics=True)
        assert without.scanned[0].risk_decision == RiskDecision.REJECT
        assert without.best is None
        _assert_scan_equivalent(without, with_)

    # --------------------------------------- 6. valid candidate survives to review
    def test_scenario_6_valid_candidate_survives_to_review(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        # A very permissive NO_TRADE hurdle forces `best` to be populated
        # once the candidate is Risk-approved -- this scenario is about
        # proving diagnostics-neutrality of the "candidate selected"
        # path structurally, not about this specific chain being
        # profitable (real profitability is a Quant/Risk concern, tested
        # elsewhere; this file only proves diagnostics never change it).
        kwargs = dict(no_trade_hurdle=-1_000.0)
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=False, **kwargs)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=True, **kwargs)
        assert without.best is not None, "this scenario requires a selected candidate to be meaningful"
        assert without.scanned[0].risk_decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)
        _assert_scan_equivalent(without, with_)

    # ------------------------------------------- 7. Covered Call prerequisite
    def test_scenario_7_covered_call_prerequisite_failure_no_shares(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        # No `underlying_holdings` entry for SPY at all -- the
        # >=100-share prerequisite fails deterministically, before any
        # chain/contract logic runs for this strategy.
        portfolio = _portfolio(5_000_000.0)
        without = _run(universe, chains, [StrategyType.COVERED_CALL], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.COVERED_CALL], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=True)
        assert without.candidates_generated == 0, "Covered Call with no shares must generate zero candidates"
        _assert_scan_equivalent(without, with_)

    # ------------------------------------------------ 8. degraded provider
    def test_scenario_8_degraded_provider_missing_chain_for_one_ticker(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF"), UniverseEntry(ticker="QQQ", sector="ETF")]
        chains = {"SPY": _spy_chain()}  # QQQ's fetch "failed" this cycle -- simply absent, never fabricated
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, _portfolio(5_000_000.0), with_diagnostics=True)
        assert {s.candidate.proposal.ticker for s in without.scanned} == {"SPY"}
        _assert_scan_equivalent(without, with_)

    # ------------------------------------------------------- 9. empty portfolio
    def test_scenario_9_empty_portfolio_no_existing_positions(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        portfolio = _portfolio(5_000_000.0)
        assert portfolio.positions == []
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=True)
        _assert_scan_equivalent(without, with_)

    # ------------------------------------------- 10. existing position
    def test_scenario_10_portfolio_with_existing_position(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_chain()}
        portfolio = _portfolio(5_000_000.0, positions=[_existing_position()])
        assert len(portfolio.positions) == 1
        without = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=False)
        with_ = _run(universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, portfolio, with_diagnostics=True)
        # post-trade exposure must be computed against the SAME existing
        # position in both runs -- a real place diagnostics could leak
        # into a decision if it were read back anywhere.
        assert without.scanned[0].post_trade_underlying_exposure_pct == with_.scanned[0].post_trade_underlying_exposure_pct
        _assert_scan_equivalent(without, with_)


class TestFunnelDiagnosticsCollectionItselfNeverAffectsDecisions:
    """A second, coarser-grained proof spanning several scenarios at
    once, run through `src.workflows.candidate_generation.generate_candidates`
    directly (one level below `scan_and_rank_opportunities`) -- the
    layer `FunnelDiagnostics` is actually recorded into."""

    def test_generate_candidates_produces_identical_proposals_with_and_without_diagnostics(self):
        from src.workflows.candidate_generation import generate_candidates

        chain = _spy_chain()
        entry = UniverseEntry(ticker="SPY", sector="ETF")
        portfolio = _portfolio(5_000_000.0)
        strategies = [StrategyType.CASH_SECURED_PUT, StrategyType.COVERED_CALL, StrategyType.PUT_CREDIT_SPREAD]

        without = generate_candidates(entry, chain, strategies, QUANT_FILTER, LIMITS, portfolio, "normal", now=NOW, diagnostics=None)
        diag = FunnelDiagnostics(ticker="SPY")
        with_ = generate_candidates(entry, chain, strategies, QUANT_FILTER, LIMITS, portfolio, "normal", now=NOW, diagnostics=diag)

        assert without == with_
        assert [c.proposal.proposal_id for c in without] == [c.proposal.proposal_id for c in with_]
        # Diagnostics collection itself must have actually happened --
        # otherwise this "equivalence" proof would be vacuous.
        assert diag.chain_contracts_seen == len(chain.contracts)
