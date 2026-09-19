"""Shared builders for Portfolio Manager (Step 10) tests."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.llm.context import PortfolioStateContext, QuantitativeAnalysisContext, RiskEngineContext
from src.llm.schemas import (
    AdversarialReview,
    Conviction,
    LegSide,
    MarketRegimeAssessment,
    OptionLeg,
    OptionRight,
    RiskReviewNote,
    StrategyType,
    TradeDirection,
    TradeProposal,
)

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
DATA_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)


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
        data_timestamp=DATA_TS,
        invalidation_conditions=["close below 610"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def make_market_regime(**overrides) -> MarketRegimeAssessment:
    base = dict(regime="normal", commentary="calm, low realized vol", notable_events=[])
    base.update(overrides)
    return MarketRegimeAssessment(**base)


def make_quant_analysis(**overrides) -> QuantitativeAnalysisContext:
    base = dict(
        max_profit=150.0,
        max_loss=850.0,
        breakeven=619.25,
        capital_required=850.0,
        return_on_capital=0.176,
        annualized_roc=2.47,
        probability_of_profit=0.63,
        expected_value=-50.0,
    )
    base.update(overrides)
    return QuantitativeAnalysisContext(**base)


def make_devil_advocate_review(**overrides) -> AdversarialReview:
    base = dict(proposal_id="prop-1", critique="reasonable structure, thin edge", risk_flags=[], do_not_advance=False)
    base.update(overrides)
    return AdversarialReview(**base)


def make_risk_reviewer_note(**overrides) -> RiskReviewNote:
    base = dict(proposal_id="prop-1", concerns=[], concurs_with_quant_review=True, note="agrees with quant sizing")
    base.update(overrides)
    return RiskReviewNote(**base)


def make_portfolio_state(**overrides) -> PortfolioStateContext:
    base = dict(nav=100_000.0, net_delta=0.0, net_theta=0.0, net_vega=0.0, current_drawdown_pct=0.0, open_position_count=0)
    base.update(overrides)
    return PortfolioStateContext(**base)


def make_risk_engine_result(**overrides) -> RiskEngineContext:
    base = dict(decision="resize", reason_codes=["resized_position_risk"], approved_contracts=2, message="resized to 2 contracts")
    base.update(overrides)
    return RiskEngineContext(**base)


def valid_portfolio_decision_input(**overrides) -> dict:
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
