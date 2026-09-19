"""Shared scenario builders for src.risk tests. Every builder returns a
fresh, independently-valid object; tests that want to break one rule
start from a full baseline scenario (`build_approved_pcs_scenario` /
`build_approved_csp_scenario` / `build_approved_covered_call_scenario`)
and use `.model_copy(update={...})` (all the relevant types are frozen
Pydantic models) or rebuild a specific piece to introduce exactly one
violation, so each test proves the engine catches that one thing rather
than an accumulation of unrelated differences from a hand-rolled
fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataOptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import (
    Conviction,
    LegSide,
    OptionLeg,
    StrategyType,
    TradeDirection,
    TradeProposal,
)
from src.llm.schemas import OptionRight as ProposalOptionRight
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.limits import RiskLimitsConfig, load_risk_limits
from src.risk.portfolio_risk import Portfolio, UnderlyingHolding
from src.risk.trade_risk import QuantitativeAnalysis, compute_trade_economics, resolve_leg_contracts

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)


@dataclass
class Scenario:
    proposal: TradeProposal
    portfolio: Portfolio
    quantitative_analysis: QuantitativeAnalysis
    market_data: OptionChain
    broker_capabilities: BrokerCapabilities | None
    limits: RiskLimitsConfig


def default_limits() -> RiskLimitsConfig:
    return load_risk_limits()


def fidelity_capabilities(**overrides) -> BrokerCapabilities:
    base = dict(
        broker_name="fidelity",
        execution_mode="MANUAL",
        account_alias="OPTIONS_ACCOUNT",
        options_enabled=True,
        allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD"],
    )
    base.update(overrides)
    return BrokerCapabilities(**base)


def make_underlying(**overrides) -> UnderlyingQuote:
    base = dict(symbol="SPY", bid=628.4, ask=628.6, last=628.5, volume=1_000_000, timestamp=MD_TS, source="mock")
    base.update(overrides)
    return UnderlyingQuote(**base)


def make_contract(**overrides) -> OptionContract:
    base = dict(
        underlying="SPY",
        option_symbol="SPY261016P00620000",
        expiration=EXPIRATION,
        strike=620.0,
        right=DataOptionRight.PUT,
        bid=1.32,
        ask=1.38,
        last=1.35,
        volume=500,
        open_interest=1000,
        iv=0.18,
        underlying_price=628.5,
        timestamp=MD_TS,
        source="mock",
    )
    base.update(overrides)
    return OptionContract(**base)


def pcs_proposal(**overrides) -> TradeProposal:
    base = dict(
        proposal_id="prop-pcs-1",
        timestamp=NOW,
        ticker="SPY",
        strategy=StrategyType.PUT_CREDIT_SPREAD,
        market_regime="normal",
        expiration=EXPIRATION,
        legs=[
            OptionLeg(right=ProposalOptionRight.PUT, strike=620.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.PUT, strike=615.0, side=LegSide.BUY),
        ],
        direction=TradeDirection.NEUTRAL,
        contracts_requested=2,
        target_entry=0.75,
        profit_target=0.5,
        management_dte=21,
        thesis="range-bound",
        risk_thesis="defined risk credit spread",
        confidence=Conviction.MEDIUM,
        data_sources=["mock"],
        data_timestamp=MD_TS,
        invalidation_conditions=["break below 610"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def csp_proposal(**overrides) -> TradeProposal:
    # A lower-priced underlying than the SPY put credit spread fixture,
    # deliberately: cash-secured-put collateral/max-loss scales with the
    # strike price (~strike x 100 per contract), so a $620 strike would
    # need a multi-million-dollar NAV before even 1 contract fits inside
    # a 1% target-risk-per-trade budget. "LOWP" keeps the fixture's
    # numbers realistic against the $100k test portfolio used throughout
    # this suite.
    base = dict(
        proposal_id="prop-csp-1",
        timestamp=NOW,
        ticker="LOWP",
        strategy=StrategyType.CASH_SECURED_PUT,
        market_regime="normal",
        expiration=EXPIRATION,
        legs=[OptionLeg(right=ProposalOptionRight.PUT, strike=9.5, side=LegSide.SELL)],
        direction=TradeDirection.BULLISH,
        contracts_requested=1,
        target_entry=0.30,
        profit_target=0.5,
        management_dte=21,
        thesis="want to own LOWP lower",
        risk_thesis="cash-secured, fine being assigned",
        confidence=Conviction.MEDIUM,
        data_sources=["mock"],
        data_timestamp=MD_TS,
        invalidation_conditions=["break below 8 with volume"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def covered_call_proposal(**overrides) -> TradeProposal:
    # Same rationale as csp_proposal: a covered call's max loss scales
    # with the underlying's cost basis, so "LOWP" keeps this fixture
    # sizeable against a $100k test portfolio.
    base = dict(
        proposal_id="prop-cc-1",
        timestamp=NOW,
        ticker="LOWP",
        strategy=StrategyType.COVERED_CALL,
        market_regime="normal",
        expiration=EXPIRATION,
        legs=[OptionLeg(right=ProposalOptionRight.CALL, strike=11.0, side=LegSide.SELL)],
        direction=TradeDirection.NEUTRAL,
        contracts_requested=1,
        target_entry=0.30,
        profit_target=0.5,
        management_dte=21,
        thesis="cap upside on existing shares",
        risk_thesis="willing to have shares called away at 11",
        confidence=Conviction.MEDIUM,
        data_sources=["mock"],
        data_timestamp=MD_TS,
        invalidation_conditions=["break above 11.5 pre-expiration"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def make_portfolio(**overrides) -> Portfolio:
    base = dict(
        as_of=NOW,
        nav=100_000.0,
        cash=90_000.0,
        peak_equity=100_000.0,
        sector_by_ticker={"SPY": "INDEX"},
    )
    base.update(overrides)
    return Portfolio(**base)


def quantitative_analysis_from(proposal: TradeProposal, contracts, portfolio, limits) -> QuantitativeAnalysis:
    econ = compute_trade_economics(proposal, contracts, portfolio, limits, num_contracts=proposal.contracts_requested)
    return QuantitativeAnalysis(
        proposal_id=proposal.proposal_id,
        generated_at=MD_TS,
        max_profit=econ.max_profit,
        max_loss=econ.max_loss,
        breakeven=econ.breakeven,
        capital_required=econ.capital_required,
        return_on_capital=econ.return_on_capital,
        annualized_roc=econ.annualized_roc,
        probability_of_profit=econ.probability_of_profit,
        expected_value=econ.expected_value,
        net_delta=0.0,
        net_vega=0.0,
    )


def build_approved_pcs_scenario(**portfolio_overrides) -> Scenario:
    proposal = pcs_proposal()
    underlying = make_underlying()
    contracts = [
        make_contract(option_symbol="SPY261016P00620000", strike=620.0, right=DataOptionRight.PUT, bid=1.32, ask=1.38, iv=0.18),
        make_contract(option_symbol="SPY261016P00615000", strike=615.0, right=DataOptionRight.PUT, bid=0.58, ask=0.62, iv=0.19),
    ]
    market_data = OptionChain(underlying=underlying, contracts=contracts, timestamp=MD_TS, source="mock")
    limits = default_limits()
    portfolio = make_portfolio(**portfolio_overrides)
    resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
    qa = quantitative_analysis_from(proposal, resolved, portfolio, limits)
    return Scenario(
        proposal=proposal,
        portfolio=portfolio,
        quantitative_analysis=qa,
        market_data=market_data,
        broker_capabilities=fidelity_capabilities(),
        limits=limits,
    )


def build_approved_csp_scenario(**portfolio_overrides) -> Scenario:
    proposal = csp_proposal()
    underlying = make_underlying(symbol="LOWP", bid=10.45, ask=10.55, last=10.50)
    contracts = [
        make_contract(
            underlying="LOWP", option_symbol="LOWP261016P00009500", strike=9.5, right=DataOptionRight.PUT,
            bid=0.29, ask=0.31, last=0.30, iv=0.35, underlying_price=10.50,
        )
    ]
    market_data = OptionChain(underlying=underlying, contracts=contracts, timestamp=MD_TS, source="mock")
    limits = default_limits()
    base_portfolio = dict(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, sector_by_ticker={"LOWP": "OTHER"})
    base_portfolio.update(portfolio_overrides)
    portfolio = make_portfolio(**base_portfolio)
    resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
    qa = quantitative_analysis_from(proposal, resolved, portfolio, limits)
    return Scenario(
        proposal=proposal,
        portfolio=portfolio,
        quantitative_analysis=qa,
        market_data=market_data,
        broker_capabilities=fidelity_capabilities(),
        limits=limits,
    )


def build_approved_covered_call_scenario(**portfolio_overrides) -> Scenario:
    proposal = covered_call_proposal()
    underlying = make_underlying(symbol="LOWP", bid=10.45, ask=10.55, last=10.50)
    contracts = [
        make_contract(
            underlying="LOWP", option_symbol="LOWP261016C00011000", strike=11.0, right=DataOptionRight.CALL,
            bid=0.29, ask=0.31, last=0.30, iv=0.32, underlying_price=10.50,
        )
    ]
    market_data = OptionChain(underlying=underlying, contracts=contracts, timestamp=MD_TS, source="mock")
    limits = default_limits()
    base_portfolio = dict(
        nav=100_000.0,
        cash=90_000.0,
        peak_equity=100_000.0,
        sector_by_ticker={"LOWP": "OTHER"},
        underlying_holdings={"LOWP": UnderlyingHolding(shares=100, cost_basis=9.0)},
    )
    base_portfolio.update(portfolio_overrides)
    portfolio = make_portfolio(**base_portfolio)
    resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
    qa = quantitative_analysis_from(proposal, resolved, portfolio, limits)
    return Scenario(
        proposal=proposal,
        portfolio=portfolio,
        quantitative_analysis=qa,
        market_data=market_data,
        broker_capabilities=fidelity_capabilities(),
        limits=limits,
    )
