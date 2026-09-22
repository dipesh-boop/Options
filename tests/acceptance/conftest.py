"""Step 21 acceptance-test infrastructure.

Deliberately reuses, rather than reimplements, the exact real
components every unit-test module already exercises individually:
`src.risk.engine.evaluate_trade_proposal` (the real, unmocked Risk
Engine), `src.brokers.order_validator.validate_and_build_order_request`
(the real Order Validator), a real `src.brokers.paper.PaperBroker`, and
`src.orchestration.pipeline.default_quant_stage` (the real Quant
stage). Only the two LLM stages (Devil's Advocate, Portfolio Manager)
are replaced with a scripted fake `LLMClient` returning a fixed,
frozen payload -- this suite never makes a live Anthropic API call and
is fully reproducible, per the instruction's "frozen/mock market
datasets, no live markets" requirement.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataRight
from src.data.quotes import UnderlyingQuote
from src.llm.client import LLMClient
from src.llm.context import MarketSnapshotContext
from src.llm.devils_advocate import evaluate_trade_risk as da_evaluate
from src.llm.portfolio_manager import evaluate_proposal as pm_evaluate
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
from src.orchestration.pipeline import (
    InMemoryDatabase,
    PipelineRequest,
    PipelineStages,
    default_portfolio_update_stage,
    default_quant_stage,
)
from src.brokers.order_validator import build_occ_symbol, validate_and_build_order_request
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.risk.broker_constraints import BrokerCapabilities, load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal as risk_evaluate
from src.risk.limits import load_risk_limits
from src.risk.portfolio_risk import Portfolio, UnderlyingHolding

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
SPOT = 628.5


def make_underlying(symbol: str = "SPY", spot: float = SPOT, **overrides) -> UnderlyingQuote:
    base = dict(symbol=symbol, bid=spot - 0.1, ask=spot + 0.1, last=spot, volume=1_000_000, timestamp=MD_TS, source="mock")
    base.update(overrides)
    return UnderlyingQuote(**base)


def make_contract(**overrides) -> OptionContract:
    base = dict(
        underlying="SPY", expiration=EXPIRATION, volume=500, open_interest=1000, iv=0.20,
        underlying_price=SPOT, timestamp=MD_TS, source="mock",
    )
    base.update(overrides)
    if "option_symbol" not in base:
        # Matches `src.brokers.order_validator.build_occ_symbol` exactly --
        # a real `ApprovedOrder`'s legs are looked up in `PaperBroker`'s
        # loaded chain by this same OCC-style symbol, so a fixture built
        # with a different convention would never actually resolve.
        base["option_symbol"] = build_occ_symbol(base["underlying"], base["expiration"], base["right"], base["strike"])
    if "last" not in base:
        base["last"] = (base["bid"] + base["ask"]) / 2.0
    return OptionContract(**base)


def make_chain(*contracts: OptionContract, symbol: str = "SPY", spot: float = SPOT) -> OptionChain:
    return OptionChain(underlying=make_underlying(symbol=symbol, spot=spot), contracts=list(contracts), timestamp=MD_TS, source="mock")


def make_portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, sector_by_ticker={"SPY": "INDEX"})
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
    base = dict(as_of=MD_TS, underlying_price=SPOT, bid=0.70, ask=0.80, iv=0.20, delta=-0.30)
    base.update(overrides)
    return MarketSnapshotContext(**base)


def _all_categories_not_applicable() -> list[dict]:
    return [{"category": c, "applicable": False, "note": "not material"} for c in sorted(_ALL_RISK_CATEGORIES)]


def da_pass_payload(**overrides) -> dict:
    base = dict(
        review_id="rev-1", proposal_id="prop-1", verdict="PASS",
        why_not_thesis="A fast adverse move still loses money despite the defined-risk structure.",
        risk_assessment=_all_categories_not_applicable(),
        failure_scenarios=[
            {"scenario": "Gap through the structure's risk boundary", "probability_category": "low", "severity": "high", "portfolio_impact_category": "moderate", "warning_indicators": ["VIX spike"], "possible_mitigation": "avoid macro releases"},
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


def da_reject_payload(**overrides) -> dict:
    payload = da_pass_payload(**overrides)
    payload["verdict"] = "REJECT"
    payload["why_not_thesis"] = "Hostile-input test scenario: this structure should never be approved."
    return payload


def pm_advance_payload(**overrides) -> dict:
    base = dict(
        decision_id="dec-1", proposal_id="prop-1", decision="propose_advance", confidence="medium",
        market_regime="normal", thesis_summary="Defined-risk structure with acceptable ROC for the stated regime.",
        bear_case="A fast adverse move breaches the structure's risk boundary before expiration.",
        portfolio_fit="Adds exposure without duplicating an existing position.",
        correlation_assessment="Low correlation to current holdings.",
        capital_efficiency="Better risk-adjusted use of capital than holding cash here.",
        alternative_considered="Holding cash instead of deploying this capital.",
        cash_preferred=False, invalidation_conditions=["Thesis invalidated"], required_follow_up=[],
        timestamp=NOW.isoformat(),
    )
    base.update(overrides)
    return base


def client_returning(payload: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))


def hostile_client(raw_text: str) -> LLMClient:
    """A fake LLM client that returns free-text, NOT a valid tool_use
    block -- simulates a model that ignored its structured-output tool
    entirely (e.g. tried to respond in prose, possibly containing a
    prompt-injection attempt) so tests can prove this fails the schema
    boundary rather than silently coercing into something trusted."""

    def _create(**kwargs):
        block = SimpleNamespace(type="text", text=raw_text)
        return SimpleNamespace(id="msg_hostile", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))


def full_stages(
    *, broker: PaperBroker | None = None, market_data: OptionChain | None = None,
    da_payload: dict | None = None, pm_payload: dict | None = None, **overrides,
) -> PipelineStages:
    if broker is None:
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
    if market_data is not None:
        broker.update_market_data(market_data)
    if "paper_broker" not in overrides:
        overrides["paper_broker"] = broker

    da_payload = da_payload if da_payload is not None else da_pass_payload()
    pm_payload = pm_payload if pm_payload is not None else pm_advance_payload()

    base = dict(
        quant_stage=lambda proposal, market_data, portfolio, limits: default_quant_stage(proposal, market_data, portfolio, limits, now=NOW),
        devils_advocate_stage=lambda inputs: da_evaluate(inputs, client=client_returning(da_payload), system_prompt="DA"),
        portfolio_manager_stage=lambda inputs: pm_evaluate(inputs, client=client_returning(pm_payload), system_prompt="PM"),
        risk_engine_stage=risk_evaluate,
        order_validator_stage=validate_and_build_order_request,
        portfolio_update_stage=default_portfolio_update_stage,
        database=InMemoryDatabase(),
    )
    base.update(overrides)
    return PipelineStages(**base)


def pipeline_request(
    *, proposal: TradeProposal, market_data: OptionChain, portfolio: Portfolio | None = None,
    allowed_strategies: list[StrategyType] | None = None, **overrides,
) -> PipelineRequest:
    """`allowed_strategies`, if given, is a list of `StrategyType` enum
    members (not raw config strings -- `model_copy` bypasses
    `BrokerCapabilities`'s own string-parsing validator, so this
    builds the already-parsed `frozenset[StrategyType]` shape directly
    rather than silently constructing a capability object whose field
    holds unparsed strings that would never compare equal to a real
    `StrategyType` member)."""
    limits = load_risk_limits()
    manual_caps = load_broker_capabilities("fidelity")
    auto_caps = load_broker_capabilities("internal_paper")
    if allowed_strategies is not None:
        allowed = frozenset(allowed_strategies)
        manual_caps = manual_caps.model_copy(update={"allowed_strategies": allowed})
        auto_caps = auto_caps.model_copy(update={"allowed_strategies": allowed})
    base = dict(
        proposal=proposal, market_data=market_data, portfolio=portfolio or make_portfolio(),
        limits=limits, market_regime=make_market_regime(), risk_reviewer_note=make_risk_reviewer_note(proposal_id=proposal.proposal_id),
        analysis_snapshot=make_snapshot(), current_snapshot=make_snapshot(),
        automated_broker_capabilities=auto_caps, manual_broker_capabilities=manual_caps, now=NOW,
    )
    base.update(overrides)
    return PipelineRequest(**base)


def base_proposal(strategy: StrategyType, legs: list[OptionLeg], **overrides) -> TradeProposal:
    base = dict(
        proposal_id="prop-1", timestamp=NOW, ticker="SPY", strategy=strategy, market_regime="normal",
        expiration=EXPIRATION, legs=legs, direction=TradeDirection.NEUTRAL, contracts_requested=1,
        target_entry=1.0, profit_target=0.5, management_dte=21, thesis="acceptance-test thesis",
        risk_thesis="acceptance-test risk thesis", confidence=Conviction.MEDIUM, data_sources=["mock"],
        data_timestamp=MD_TS, invalidation_conditions=["thesis invalidated"],
    )
    base.update(overrides)
    return TradeProposal(**base)


@dataclass(frozen=True)
class StrategyFixture:
    """Proposal legs + matching option contracts + a portfolio suited
    to that strategy (shares-holding strategies get a share position;
    others get the plain cash portfolio), for every one of the 15
    order-eligible `StrategyType` members. CASH/NO_TRADE is
    deliberately not a `StrategyType` member (see
    `src.strategies.base.StrategyKind`'s own docstring) and has no
    fixture here -- it is tested separately in test_strategy_integrity.py
    as "the Risk Engine can produce zero approvable candidates," not as
    a structure to price."""

    strategy: StrategyType
    legs: list[OptionLeg]
    contracts: list[OptionContract]
    portfolio: Portfolio


def _share_portfolio(**overrides) -> Portfolio:
    # cost_basis=620 sits between the protective put's 610 strike and
    # spot (628.5) so protective put/collar have real, positive defined
    # risk (cost_basis below the put strike would mean exercising it is
    # a gain, not a loss, correctly sizing to zero) while still being a
    # sensible covered-call cost basis (below the 650 call strike).
    overrides.setdefault("underlying_holdings", {"SPY": UnderlyingHolding(shares=100, cost_basis=620.0)})
    return make_portfolio(**overrides)


def _large_portfolio(**overrides) -> Portfolio:
    """A SPY-priced cash-secured put's collateral (~strike x 100 =
    ~$62,000/contract) needs a large enough NAV for the Risk Engine's
    1%-of-NAV target-risk-per-trade budget to size at least one
    contract -- this is the Risk Engine correctly sizing down, not a
    bug, so the fixture uses a realistically large account instead of
    loosening any risk limit."""
    overrides.setdefault("nav", 10_000_000.0)
    overrides.setdefault("cash", 9_500_000.0)
    overrides.setdefault("peak_equity", 10_000_000.0)
    return make_portfolio(**overrides)


def all_strategy_fixtures() -> dict[StrategyType, StrategyFixture]:
    P, C = OptionRight.PUT, OptionRight.CALL
    DP, DC = DataRight.PUT, DataRight.CALL
    plain = make_portfolio()
    large = _large_portfolio()
    shares = _share_portfolio()
    large_shares = _share_portfolio(nav=10_000_000.0, cash=9_500_000.0, peak_equity=10_000_000.0)

    fixtures: dict[StrategyType, StrategyFixture] = {}

    fixtures[StrategyType.CASH_SECURED_PUT] = StrategyFixture(
        StrategyType.CASH_SECURED_PUT,
        [OptionLeg(right=P, strike=620.0, side=LegSide.SELL)],
        [make_contract(strike=620.0, right=DP, bid=1.32, ask=1.38)],
        large,
    )
    fixtures[StrategyType.COVERED_CALL] = StrategyFixture(
        StrategyType.COVERED_CALL,
        [OptionLeg(right=C, strike=650.0, side=LegSide.SELL)],
        [make_contract(strike=650.0, right=DC, bid=1.9, ask=2.0)],
        large_shares,
    )
    fixtures[StrategyType.PUT_CREDIT_SPREAD] = StrategyFixture(
        StrategyType.PUT_CREDIT_SPREAD,
        [OptionLeg(right=P, strike=620.0, side=LegSide.SELL), OptionLeg(right=P, strike=615.0, side=LegSide.BUY)],
        [make_contract(strike=620.0, right=DP, bid=1.32, ask=1.38),
         make_contract(strike=615.0, right=DP, bid=0.58, ask=0.62)],
        plain,
    )
    fixtures[StrategyType.CALL_CREDIT_SPREAD] = StrategyFixture(
        StrategyType.CALL_CREDIT_SPREAD,
        [OptionLeg(right=C, strike=640.0, side=LegSide.SELL), OptionLeg(right=C, strike=650.0, side=LegSide.BUY)],
        [make_contract(strike=640.0, right=DC, bid=2.8, ask=3.0),
         make_contract(strike=650.0, right=DC, bid=1.15, ask=1.25)],
        plain,
    )
    fixtures[StrategyType.BULL_CALL_SPREAD] = StrategyFixture(
        StrategyType.BULL_CALL_SPREAD,
        [OptionLeg(right=C, strike=620.0, side=LegSide.BUY), OptionLeg(right=C, strike=630.0, side=LegSide.SELL)],
        [make_contract(strike=620.0, right=DC, bid=10.8, ask=11.0),
         make_contract(strike=630.0, right=DC, bid=4.3, ask=4.5)],
        plain,
    )
    fixtures[StrategyType.BEAR_PUT_SPREAD] = StrategyFixture(
        StrategyType.BEAR_PUT_SPREAD,
        [OptionLeg(right=P, strike=630.0, side=LegSide.BUY), OptionLeg(right=P, strike=620.0, side=LegSide.SELL)],
        [make_contract(strike=630.0, right=DP, bid=6.0, ask=6.2),
         make_contract(strike=620.0, right=DP, bid=2.0, ask=2.2)],
        plain,
    )
    fixtures[StrategyType.PROTECTIVE_PUT] = StrategyFixture(
        StrategyType.PROTECTIVE_PUT,
        [OptionLeg(right=P, strike=610.0, side=LegSide.BUY)],
        [make_contract(strike=610.0, right=DP, bid=2.4, ask=2.6)],
        shares,
    )
    fixtures[StrategyType.PROTECTIVE_COLLAR] = StrategyFixture(
        StrategyType.PROTECTIVE_COLLAR,
        [OptionLeg(right=C, strike=650.0, side=LegSide.SELL), OptionLeg(right=P, strike=610.0, side=LegSide.BUY)],
        [make_contract(strike=650.0, right=DC, bid=1.9, ask=2.1),
         make_contract(strike=610.0, right=DP, bid=1.4, ask=1.6)],
        shares,
    )
    fixtures[StrategyType.LONG_STRADDLE] = StrategyFixture(
        StrategyType.LONG_STRADDLE,
        [OptionLeg(right=C, strike=628.0, side=LegSide.BUY), OptionLeg(right=P, strike=628.0, side=LegSide.BUY)],
        [make_contract(strike=628.0, right=DC, bid=12.0, ask=12.4),
         make_contract(strike=628.0, right=DP, bid=12.1, ask=12.5)],
        plain,
    )
    fixtures[StrategyType.LONG_STRANGLE] = StrategyFixture(
        StrategyType.LONG_STRANGLE,
        [OptionLeg(right=C, strike=640.0, side=LegSide.BUY), OptionLeg(right=P, strike=615.0, side=LegSide.BUY)],
        [make_contract(strike=640.0, right=DC, bid=5.8, ask=6.0),
         make_contract(strike=615.0, right=DP, bid=5.8, ask=6.0)],
        plain,
    )
    fixtures[StrategyType.LONG_CALL] = StrategyFixture(
        StrategyType.LONG_CALL,
        [OptionLeg(right=C, strike=630.0, side=LegSide.BUY)],
        [make_contract(strike=630.0, right=DC, bid=6.8, ask=7.0)],
        plain,
    )
    fixtures[StrategyType.LONG_PUT] = StrategyFixture(
        StrategyType.LONG_PUT,
        [OptionLeg(right=P, strike=610.0, side=LegSide.BUY)],
        [make_contract(strike=610.0, right=DP, bid=2.5, ask=2.7)],
        plain,
    )
    fixtures[StrategyType.LONG_CALL_BUTTERFLY] = StrategyFixture(
        StrategyType.LONG_CALL_BUTTERFLY,
        [OptionLeg(right=C, strike=615.0, side=LegSide.BUY, quantity_ratio=1),
         OptionLeg(right=C, strike=628.0, side=LegSide.SELL, quantity_ratio=2),
         OptionLeg(right=C, strike=641.0, side=LegSide.BUY, quantity_ratio=1)],
        [make_contract(strike=615.0, right=DC, bid=16.4, ask=16.6),
         make_contract(strike=628.0, right=DC, bid=9.4, ask=9.6),
         make_contract(strike=641.0, right=DC, bid=4.4, ask=4.6)],
        plain,
    )
    fixtures[StrategyType.SHORT_IRON_CONDOR] = StrategyFixture(
        StrategyType.SHORT_IRON_CONDOR,
        [OptionLeg(right=P, strike=600.0, side=LegSide.BUY), OptionLeg(right=P, strike=610.0, side=LegSide.SELL),
         OptionLeg(right=C, strike=650.0, side=LegSide.SELL), OptionLeg(right=C, strike=660.0, side=LegSide.BUY)],
        [make_contract(strike=600.0, right=DP, bid=0.55, ask=0.60),
         make_contract(strike=610.0, right=DP, bid=1.20, ask=1.30),
         make_contract(strike=650.0, right=DC, bid=1.15, ask=1.20),
         make_contract(strike=660.0, right=DC, bid=0.47, ask=0.50)],
        plain,
    )
    fixtures[StrategyType.SHORT_IRON_BUTTERFLY] = StrategyFixture(
        StrategyType.SHORT_IRON_BUTTERFLY,
        [OptionLeg(right=P, strike=610.0, side=LegSide.BUY), OptionLeg(right=P, strike=628.0, side=LegSide.SELL),
         OptionLeg(right=C, strike=628.0, side=LegSide.SELL), OptionLeg(right=C, strike=646.0, side=LegSide.BUY)],
        [make_contract(strike=610.0, right=DP, bid=0.85, ask=0.90),
         make_contract(strike=628.0, right=DP, bid=3.2, ask=3.3),
         make_contract(strike=628.0, right=DC, bid=3.4, ask=3.5),
         make_contract(strike=646.0, right=DC, bid=0.95, ask=1.0)],
        plain,
    )
    return fixtures


# Step 22.4B: the shared "what counts as repository-controlled" helper
# every repo-wide security/portability scan in tests/acceptance/ should
# use instead of a bare `REPO_ROOT.rglob(...)`. A raw rglob walks the
# actual filesystem, so on a real operator checkout it also descends
# into `.venv/`/`venv/` (installed third-party packages -- e.g. the
# `alpaca-py` dependency itself imports `alpaca.trading` internally,
# which a naive scan would misreport as a repository-source violation)
# and matches the operator's own gitignored `.env`. `git ls-files
# --cached --others --exclude-standard` is git's own authoritative
# definition of "tracked, or untracked-but-not-ignored" -- exactly
# "could end up committed" -- so a file only ever gets excluded here
# because git itself, via `.gitignore`, says it's not repository-
# controlled, never because this helper guessed a directory name. A
# credential file that ever became tracked (staged/committed) would
# immediately reappear in this list and correctly fail the check that
# uses it -- this is a stronger guarantee than a hand-maintained
# exclusion list of directory names, not a weaker one.
def repo_controlled_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    )
    return [root / rel for rel in result.stdout.decode("utf-8", errors="ignore").split("\0") if rel]
