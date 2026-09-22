from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.brokers.order_validator import build_occ_symbol
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import (
    Conviction,
    LegSide,
    OptionLeg,
    StrategyType,
    TradeDirection,
)
from src.llm.schemas import OptionRight as SchemaOptionRight
from src.llm.schemas import TradeProposal
from src.orchestration.pipeline import default_quant_stage
from src.review.candidates import CandidateStatus, ReviewedCandidate
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXPIRATION = (NOW + timedelta(days=30)).date()
LIMITS = get_default_limits()
BROKER_CAPS = load_broker_capabilities("internal_paper")


def make_chain(*, strike: float = 5.0, underlying_px: float = 10.0, bid: float = 0.48, ask: float = 0.52, as_of: datetime = NOW) -> OptionChain:
    """A deliberately small-strike, tight-spread contract -- easily
    Risk-approved against a $100k account, and tight enough that
    PaperBroker's own liquidity-adjusted fill model clears the resulting
    limit price (see src.brokers.paper.compute_fill)."""
    underlying = UnderlyingQuote(symbol="SPY", bid=underlying_px - 0.05, ask=underlying_px + 0.05, last=underlying_px, volume=1_000_000, timestamp=as_of, source="test")
    # A real OCC-style symbol -- src.brokers.order_validator._build_leg
    # constructs the SAME symbol from the ApprovedOrder's ticker/expiration/
    # right/strike (never from anything on the OptionContract itself), so
    # PaperBroker's own _find_contract lookup only succeeds if this
    # fixture's symbol matches that construction exactly.
    contract = OptionContract(
        option_symbol=build_occ_symbol("SPY", EXPIRATION, OptionRight.PUT, strike), underlying="SPY", strike=strike,
        expiration=EXPIRATION, right=OptionRight.PUT, bid=bid, ask=ask, last=(bid + ask) / 2, volume=500,
        open_interest=1000, delta=-0.2, iv=0.22, underlying_price=underlying_px, timestamp=as_of, source="test",
    )
    return OptionChain(underlying=underlying, contracts=[contract], timestamp=as_of, source="test")


def make_proposal(*, proposal_id: str = "cand-1", strike: float = 5.0, target_entry: float = 0.5) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id, timestamp=NOW, ticker="SPY", strategy=StrategyType.CASH_SECURED_PUT,
        market_regime="normal", expiration=EXPIRATION, legs=[OptionLeg(right=SchemaOptionRight.PUT, strike=strike, side=LegSide.SELL)],
        direction=TradeDirection.NEUTRAL, contracts_requested=1, target_entry=target_entry, profit_target=0.5,
        management_dte=21, thesis="t", risk_thesis="rt", confidence=Conviction.MEDIUM, data_sources=["test"],
        data_timestamp=NOW, invalidation_conditions=["x"],
    )


def make_portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)
    base.update(overrides)
    return Portfolio(**base)


def make_candidate(
    *, candidate_id: str = "cand-1", cohort_id: str = "cohort-1", cycle_id: str = "cycle-1",
    chain: OptionChain | None = None, portfolio: Portfolio | None = None, proposal: TradeProposal | None = None,
    ttl_seconds: int = 900, created_at: datetime = NOW, status: CandidateStatus = CandidateStatus.AWAITING_HUMAN,
) -> ReviewedCandidate:
    chain = chain or make_chain()
    portfolio = portfolio or make_portfolio()
    proposal = proposal or make_proposal(proposal_id=candidate_id)
    qa = default_quant_stage(proposal, chain, portfolio, LIMITS, now=NOW)
    risk = evaluate_trade_proposal(proposal, portfolio, qa, chain, BROKER_CAPS, limits=LIMITS, now=NOW)
    return ReviewedCandidate(
        candidate_id=candidate_id, cohort_id=cohort_id, cycle_id=cycle_id, created_at=created_at,
        ttl_seconds=ttl_seconds, proposal=proposal, quantitative_analysis=qa, risk_decision=risk,
        entry_delta=-0.2, entry_iv=0.22, quote_timestamp=chain.timestamp, market_data_source=chain.source,
        portfolio_exposure_before_pct=0.0, portfolio_exposure_after_pct=0.01,
        sector_exposure_before_pct=0.0, sector_exposure_after_pct=0.01,
        management_policy_name="default", status=status,
    )
