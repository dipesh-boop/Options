"""Shared builders for src.orchestration tests."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataRight
from src.data.quotes import UnderlyingQuote
from src.llm.client import LLMClient
from src.llm.context import MarketSnapshotContext
from src.llm.schemas import (
    Conviction,
    LegSide,
    MarketRegimeAssessment,
    OptionLeg,
    OptionRight,
    RiskReviewNote,
    StrategyType,
    TradeDirection,
    TradeProposal,
    _ALL_RISK_CATEGORIES,
)
from src.risk.portfolio_risk import Portfolio

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
SHORT_SYMBOL = "SPY261016P00620000"
LONG_SYMBOL = "SPY261016P00615000"


def make_proposal(**overrides) -> TradeProposal:
    base = dict(
        proposal_id="prop-1",
        timestamp=NOW,
        ticker="SPY",
        strategy=StrategyType.PUT_CREDIT_SPREAD,
        market_regime="normal",
        expiration=EXPIRATION,
        legs=[
            OptionLeg(right=OptionRight.PUT, strike=620.0, side=LegSide.SELL),
            OptionLeg(right=OptionRight.PUT, strike=615.0, side=LegSide.BUY),
        ],
        direction=TradeDirection.NEUTRAL,
        contracts_requested=2,
        target_entry=0.75,
        profit_target=0.5,
        management_dte=21,
        thesis="range-bound, sell premium",
        risk_thesis="defined risk credit spread",
        confidence=Conviction.MEDIUM,
        data_sources=["mock"],
        data_timestamp=MD_TS,
        invalidation_conditions=["close below 610"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def make_market_data(**overrides) -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=628.4, ask=628.6, last=628.5, volume=1_000_000, timestamp=MD_TS, source="mock")
    contracts = [
        OptionContract(
            underlying="SPY", option_symbol=SHORT_SYMBOL, expiration=EXPIRATION, strike=620.0, right=DataRight.PUT,
            bid=1.32, ask=1.38, last=1.35, volume=500, open_interest=1000, iv=0.18, underlying_price=628.5,
            timestamp=MD_TS, source="mock",
        ),
        OptionContract(
            underlying="SPY", option_symbol=LONG_SYMBOL, expiration=EXPIRATION, strike=615.0, right=DataRight.PUT,
            bid=0.58, ask=0.62, last=0.60, volume=400, open_interest=800, iv=0.19, underlying_price=628.5,
            timestamp=MD_TS, source="mock",
        ),
    ]
    base = dict(underlying=underlying, contracts=contracts, timestamp=MD_TS, source="mock")
    base.update(overrides)
    return OptionChain(**base)


def make_portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, sector_by_ticker={"SPY": "INDEX"})
    base.update(overrides)
    return Portfolio(**base)


def make_market_regime(**overrides) -> MarketRegimeAssessment:
    base = dict(regime="normal", commentary="calm", notable_events=[])
    base.update(overrides)
    return MarketRegimeAssessment(**base)


def make_risk_reviewer_note(**overrides) -> RiskReviewNote:
    base = dict(proposal_id="prop-1", concerns=[], concurs_with_quant_review=True, note="agrees with quant sizing")
    base.update(overrides)
    return RiskReviewNote(**base)


def make_snapshot(**overrides) -> MarketSnapshotContext:
    base = dict(as_of=MD_TS, underlying_price=628.5, bid=0.70, ask=0.80, iv=0.18, delta=-0.30)
    base.update(overrides)
    return MarketSnapshotContext(**base)


def da_pass_payload(**overrides) -> dict:
    base = dict(
        review_id="rev-1",
        proposal_id="prop-1",
        verdict="PASS",
        why_not_thesis="A fast reversal below breakeven still loses money despite the defined-risk structure.",
        risk_assessment=[{"category": c, "applicable": False, "note": "not material"} for c in sorted(_ALL_RISK_CATEGORIES)],
        failure_scenarios=[
            {"scenario": "Gap down through short strike", "probability_category": "low", "severity": "high", "portfolio_impact_category": "moderate", "warning_indicators": ["VIX spike"], "possible_mitigation": "avoid macro releases"},
            {"scenario": "IV expansion", "probability_category": "medium", "severity": "medium", "portfolio_impact_category": "minor", "warning_indicators": ["VIX rising"], "possible_mitigation": "size down"},
            {"scenario": "Assignment near ex-div", "probability_category": "low", "severity": "low", "portfolio_impact_category": "negligible", "warning_indicators": ["div date near expiration"], "possible_mitigation": "close before ex-div"},
        ],
        fidelity_execution_risk={
            "underlying_movement_risk": "low", "spread_movement_risk": "low", "bid_ask_widening_risk": "low",
            "iv_change_risk": "low", "delta_change_risk": "low", "regime_change_risk": "low", "news_event_risk": "low",
            "reprice_required": False,
        },
        timestamp=NOW.isoformat(),
    )
    base.update(overrides)
    return base


def pm_advance_payload(**overrides) -> dict:
    base = dict(
        decision_id="dec-1",
        proposal_id="prop-1",
        decision="propose_advance",
        confidence="medium",
        market_regime="normal",
        thesis_summary="Range-bound regime, defined-risk credit spread with acceptable ROC.",
        bear_case="A fast move through 615 before expiration breaches the spread.",
        portfolio_fit="Adds SPY exposure without duplicating an existing position.",
        correlation_assessment="Low correlation to current holdings.",
        capital_efficiency="Better risk-adjusted use of capital than holding cash here.",
        alternative_considered="Holding cash instead of deploying this capital.",
        cash_preferred=False,
        invalidation_conditions=["Close below 615 pre-expiration"],
        required_follow_up=[],
        timestamp=NOW.isoformat(),
    )
    base.update(overrides)
    return base


def client_returning(payload: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
