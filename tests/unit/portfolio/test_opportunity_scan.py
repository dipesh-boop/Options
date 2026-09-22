"""Tests for `src.portfolio.opportunity_scan` (Step 22.4 Parts 23-24)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio.opportunity_scan import scan_and_rank_opportunities
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.risk.reason_codes import RiskDecision
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()
BROKER_CAPS = load_broker_capabilities("internal_paper")


def _spy_chain() -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1_000_000, timestamp=NOW, source="tradier")
    contracts = [
        OptionContract(
            underlying="SPY", option_symbol=f"SPY{EXP.isoformat()}P{int(strike*1000):08d}", expiration=EXP, strike=strike,
            right=OptionRight.PUT, bid=3.0, ask=3.2, last=3.1, volume=vol, open_interest=oi,
            delta=delta, iv=0.18, underlying_price=455.0, timestamp=NOW, source="tradier",
        )
        for strike, delta, oi, vol in [(450.0, -0.20, 1000, 500), (445.0, -0.12, 800, 300), (440.0, -0.08, 600, 200)]
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=NOW, source="tradier")


def _portfolio(nav: float) -> Portfolio:
    return Portfolio(as_of=NOW, nav=nav, cash=nav, peak_equity=nav)


class TestScanAndRankOpportunities:
    def test_missing_chain_for_a_ticker_is_silently_skipped(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF"), UniverseEntry(ticker="QQQ", sector="ETF")]
        result = scan_and_rank_opportunities(
            universe, {"SPY": _spy_chain()}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(5_000_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert {s.candidate.proposal.ticker for s in result.scanned} == {"SPY"}

    def test_empty_universe_yields_no_candidates_and_no_trade(self):
        result = scan_and_rank_opportunities(
            [], {}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(100_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert result.candidates_generated == 0
        assert result.best is None

    def test_undersized_portfolio_produces_risk_rejected_candidate(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        result = scan_and_rank_opportunities(
            universe, {"SPY": _spy_chain()}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(100_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert result.candidates_generated == 1
        assert result.scanned[0].risk_decision == RiskDecision.REJECT
        assert result.best is None

    def test_risk_approved_candidate_with_negative_ev_still_yields_cash_no_trade(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        result = scan_and_rank_opportunities(
            universe, {"SPY": _spy_chain()}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(5_000_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert result.candidates_generated == 1
        assert result.scanned[0].risk_decision == RiskDecision.APPROVE
        # cash is a valid, first-class outcome when nothing clears the hurdle
        assert result.best is None
        assert "NO_TRADE hurdle" in result.no_trade_reason

    def test_post_trade_exposure_is_computed_against_current_portfolio(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        result = scan_and_rank_opportunities(
            universe, {"SPY": _spy_chain()}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(5_000_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert result.scanned[0].post_trade_underlying_exposure_pct > 0

    def test_candidates_rejected_property_counts_non_approved(self):
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        result = scan_and_rank_opportunities(
            universe, {"SPY": _spy_chain()}, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(min_dte=20, max_dte=45),
            LIMITS, _portfolio(100_000.0), "normal", BROKER_CAPS, now=NOW,
        )
        assert result.candidates_rejected == 1
