"""Shared builders for Portfolio Manager (Step 10) tests."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.llm.context import (
    FidelityPracticalityContext,
    MarketSnapshotContext,
    OverfittingGuardContext,
    PerformanceBreakdownContext,
    PortfolioStateContext,
    QuantitativeAnalysisContext,
    RiskEngineContext,
)
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
    _ALL_RISK_CATEGORIES,
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


def make_market_snapshot(**overrides) -> MarketSnapshotContext:
    base = dict(as_of=DATA_TS, underlying_price=628.5, bid=0.70, ask=0.80, iv=0.18, delta=-0.30)
    base.update(overrides)
    return MarketSnapshotContext(**base)


def valid_risk_assessment(**category_overrides: dict) -> list[dict]:
    """All 18 categories, each `applicable=False` by default; pass e.g.
    `earnings={"applicable": True, "note": "..."}` to override one."""
    out = []
    for category in sorted(_ALL_RISK_CATEGORIES):
        entry = {"category": category, "applicable": False, "note": "not a material concern for this trade"}
        entry.update(category_overrides.get(category, {}))
        out.append(entry)
    return out


def valid_failure_scenarios() -> list[dict]:
    return [
        {
            "scenario": "Gap down through the short strike overnight on macro news",
            "probability_category": "low",
            "severity": "high",
            "portfolio_impact_category": "moderate",
            "warning_indicators": ["VIX spike", "pre-market gap beyond 1%"],
            "possible_mitigation": "avoid holding through major scheduled macro releases",
        },
        {
            "scenario": "Implied volatility expands, working against the short premium before theta catches up",
            "probability_category": "medium",
            "severity": "medium",
            "portfolio_impact_category": "minor",
            "warning_indicators": ["VIX term structure inverting"],
            "possible_mitigation": "size down ahead of known volatility catalysts",
        },
        {
            "scenario": "Early assignment on the short leg near an ex-dividend date",
            "probability_category": "low",
            "severity": "low",
            "portfolio_impact_category": "negligible",
            "warning_indicators": ["dividend date falls before expiration"],
            "possible_mitigation": "close or roll before the ex-dividend date",
        },
    ]


def valid_fidelity_execution_risk(**overrides) -> dict:
    base = dict(
        underlying_movement_risk="low, tight intraday range so far",
        spread_movement_risk="stable, no signs of widening",
        bid_ask_widening_risk="tight two-sided market",
        iv_change_risk="low, no scheduled vol catalysts before likely entry",
        delta_change_risk="low, underlying not near the strike",
        regime_change_risk="no scheduled regime-moving events before likely entry",
        news_event_risk="none scheduled",
        reprice_required=False,
    )
    base.update(overrides)
    return base


def valid_devils_advocate_review_input(**overrides) -> dict:
    base = dict(
        review_id="rev-1",
        proposal_id="prop-1",
        verdict="PASS",
        why_not_thesis="A fast reversal through the short strike before expiration still loses money despite the defined-risk structure.",
        risk_assessment=valid_risk_assessment(),
        failure_scenarios=valid_failure_scenarios(),
        fidelity_execution_risk=valid_fidelity_execution_risk(),
        timestamp=NOW.isoformat(),
    )
    base.update(overrides)
    return base


def make_performance_breakdown_context(**overrides) -> PerformanceBreakdownContext:
    base = dict(
        dimension="delta",
        buckets=[
            {"bucket": "15-20", "trade_count": 40, "win_rate": 0.7, "total_pnl": 1200.0, "average_pnl": 30.0, "best_pnl": 90.0, "worst_pnl": -60.0},
            {"bucket": "25-30", "trade_count": 35, "win_rate": 0.6, "total_pnl": 800.0, "average_pnl": 22.9, "best_pnl": 85.0, "worst_pnl": -70.0},
        ],
        total_trades=75,
    )
    base.update(overrides)
    return PerformanceBreakdownContext(**base)


def make_overfitting_guard_context(**overrides) -> OverfittingGuardContext:
    base = dict(
        hypotheses_tested=3,
        hypotheses_rejected=1,
        surviving_validation=2,
        surviving_out_of_sample=1,
        warnings=["survivorship bias: no confirmed survivorship-bias-free historical data vendor is wired up yet"],
    )
    base.update(overrides)
    return OverfittingGuardContext(**base)


def make_fidelity_practicality_context(**overrides) -> FidelityPracticalityContext:
    base = dict(rating="LOW", burden_score=1, reasons=["burden score 1"], hard_rejected=False)
    base.update(overrides)
    return FidelityPracticalityContext(**base)


def valid_strategy_research_review_input(**overrides) -> dict:
    base = dict(
        review_id="research-1",
        hypothesis_id="hyp-1",
        hypothesis_statement="15-20 delta put credit spreads outperform 25-30 delta spreads during high-IV regimes.",
        supporting_dimensions=["delta", "iv_percentile"],
        observation_summary="The 15-20 delta bucket shows a higher win rate and higher average P&L than the 25-30 delta bucket across the sampled trades.",
        overfitting_concerns=[],
        fidelity_practicality_rating="LOW",
        fidelity_practicality_commentary="Two-leg spread, low adjustment frequency, workable for manual Fidelity entry.",
        recommendation="proceed_to_out_of_sample",
        rationale="Validation-period results are consistent with the observation and the sample size clears the significance threshold.",
        risks_identified=["Result may be regime-dependent; confirm out-of-sample across a lower-IV period too."],
        timestamp=NOW.isoformat(),
    )
    base.update(overrides)
    return base


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
