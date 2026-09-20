"""Shared fixtures for the /morning-scan workflow test suite."""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from src.brokers.order_validator import validate_and_build_order_request
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataRight
from src.data.quotes import UnderlyingQuote
from src.llm.client import LLMClient
from src.llm.context import MarketSnapshotContext
from src.llm.devils_advocate import evaluate_trade_risk as da_evaluate
from src.llm.portfolio_manager import evaluate_proposal as pm_evaluate
from src.llm.schemas import MarketRegimeAssessment, RiskReviewNote, StrategyType, _ALL_RISK_CATEGORIES
from src.orchestration.pipeline import InMemoryDatabase, PipelineStages, default_portfolio_update_stage, default_quant_stage
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal as risk_evaluate
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = NOW
EXPIRATION = date(2026, 10, 16)


def make_underlying(symbol: str = "XYZ", price: float = 100.0) -> UnderlyingQuote:
    return UnderlyingQuote(symbol=symbol, bid=price - 0.1, ask=price + 0.1, last=price, volume=1_000_000, timestamp=MD_TS, source="mock")


def make_put(strike: float, delta: float, *, bid: float, ask: float, symbol: str = "XYZ", expiration: date = EXPIRATION, oi: int = 1000, vol: int = 500) -> OptionContract:
    return OptionContract(
        underlying=symbol, option_symbol=f"{symbol}_P{strike:g}", expiration=expiration, strike=strike, right=DataRight.PUT,
        bid=bid, ask=ask, last=(bid + ask) / 2, volume=vol, open_interest=oi, iv=0.22, delta=delta, underlying_price=100.0,
        timestamp=MD_TS, source="mock",
    )


def make_call(strike: float, delta: float, *, bid: float, ask: float, symbol: str = "XYZ", expiration: date = EXPIRATION, oi: int = 1000, vol: int = 500) -> OptionContract:
    return OptionContract(
        underlying=symbol, option_symbol=f"{symbol}_C{strike:g}", expiration=expiration, strike=strike, right=DataRight.CALL,
        bid=bid, ask=ask, last=(bid + ask) / 2, volume=vol, open_interest=oi, iv=0.20, delta=delta, underlying_price=100.0,
        timestamp=MD_TS, source="mock",
    )


def make_chain(contracts: list[OptionContract], *, symbol: str = "XYZ", price: float = 100.0, timestamp: datetime = MD_TS) -> OptionChain:
    return OptionChain(underlying=make_underlying(symbol, price), contracts=contracts, timestamp=timestamp, source="mock")


def default_pcs_chain(symbol: str = "XYZ") -> OptionChain:
    return make_chain(
        [
            make_put(95.0, -0.20, bid=1.95, ask=2.05, symbol=symbol),
            make_put(90.0, -0.10, bid=0.97, ask=1.03, symbol=symbol),
        ],
        symbol=symbol,
    )


def make_portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=95_000.0, peak_equity=100_000.0, sector_by_ticker={"XYZ": "TECH"})
    base.update(overrides)
    return Portfolio(**base)


def make_market_regime(**overrides) -> MarketRegimeAssessment:
    base = dict(regime="normal", commentary="calm, range-bound", notable_events=[])
    base.update(overrides)
    return MarketRegimeAssessment(**base)


def make_risk_reviewer_note(proposal) -> RiskReviewNote:
    return RiskReviewNote(proposal_id=proposal.proposal_id, concerns=[], concurs_with_quant_review=True, note="agrees with quant sizing")


def make_snapshot(chain: OptionChain, candidate) -> MarketSnapshotContext:
    return MarketSnapshotContext(as_of=chain.timestamp, underlying_price=chain.underlying.mid, bid=1.95, ask=2.05, iv=0.22, delta=candidate.entry_delta)


def client_returning(payload: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))


def da_pass_payload(proposal_id: str) -> dict:
    return dict(
        review_id="rev-1", proposal_id=proposal_id, verdict="PASS",
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


def da_reject_payload(proposal_id: str) -> dict:
    payload = da_pass_payload(proposal_id)
    payload["verdict"] = "REJECT"
    payload["why_not_thesis"] = "Unacceptable tail risk into a binary event before expiration."
    return payload


def pm_advance_payload(proposal_id: str) -> dict:
    return dict(
        decision_id="dec-1", proposal_id=proposal_id, decision="propose_advance", confidence="medium", market_regime="normal",
        thesis_summary="Range-bound regime, defined-risk credit spread with acceptable ROC.",
        bear_case="A fast move through the long strike before expiration breaches the spread.",
        portfolio_fit="Adds exposure without duplicating an existing position.",
        correlation_assessment="Low correlation to current holdings.",
        capital_efficiency="Better risk-adjusted use of capital than holding cash here.",
        alternative_considered="Holding cash instead of deploying this capital.",
        cash_preferred=False, invalidation_conditions=["close beyond long strike pre-expiration"], required_follow_up=[],
        timestamp=NOW.isoformat(),
    )


def pm_hold_cash_payload(proposal_id: str) -> dict:
    payload = pm_advance_payload(proposal_id)
    payload.update(decision="hold_cash", cash_preferred=True, thesis_summary="Better to preserve optionality and hold cash here.")
    return payload


def full_stages(*, chain: OptionChain, da_payload_factory=da_pass_payload, pm_payload_factory=pm_advance_payload, **overrides) -> PipelineStages:
    if "paper_broker" not in overrides:
        broker = PaperBroker(initial_cash=100_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        broker.update_market_data(chain)
        overrides["paper_broker"] = broker

    base = dict(
        quant_stage=lambda proposal, market_data, portfolio, limits: default_quant_stage(proposal, market_data, portfolio, limits, now=NOW),
        devils_advocate_stage=lambda inputs: da_evaluate(inputs, client=client_returning(da_payload_factory(inputs.proposal.proposal_id)), system_prompt="DA"),
        portfolio_manager_stage=lambda inputs: pm_evaluate(inputs, client=client_returning(pm_payload_factory(inputs.proposal.proposal_id)), system_prompt="PM"),
        risk_engine_stage=risk_evaluate,
        order_validator_stage=validate_and_build_order_request,
        portfolio_update_stage=default_portfolio_update_stage,
        database=InMemoryDatabase(),
    )
    base.update(overrides)
    return PipelineStages(**base)


def automated_capabilities():
    return load_broker_capabilities("internal_paper")


def manual_capabilities():
    return load_broker_capabilities("fidelity")
