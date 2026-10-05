"""PAPER_TRADING_V1.5.8 regression tests: confirmation's refresh must
target the candidate's EXACT persisted expiration and fail closed -- never
substitute a nearby one -- whenever the exact data cannot be positively
resolved and verified. See src.review.confirmation's module docstring for
the full architectural rationale.

All market data here is synthetic/offline (FakeProvider-style doubles) --
no Tradier call, no production or active-cohort database is touched
anywhere in this file, per PAPER_TRADING_V1.5.8's own explicit constraints.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.brokers.order_validator import build_occ_symbol
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import Conviction, LegSide, OptionLeg, StrategyType, TradeDirection, TradeProposal
from src.llm.schemas import OptionRight as SchemaOptionRight
from src.orchestration.pipeline import default_quant_stage
from src.portfolio.account_state import InMemoryPaperAccountStateStore, InMemoryPortfolioStore
from src.review.candidates import CandidateStatus, InMemoryCandidateReviewStore, ReviewedCandidate
from src.review.confirmation import ConfirmationOutcome, ConfirmCandidateInputs, confirm_candidate
from src.risk.engine import evaluate_trade_proposal
from src.validation.session import InMemoryValidationStore
from tests.unit.review.conftest import BROKER_CAPS, LIMITS, make_portfolio

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXPIRATION = (NOW + timedelta(days=45)).date()  # deliberately beyond a small nearest-N window
WRONG_EXPIRATION = (NOW + timedelta(days=3)).date()  # what an old nearest-N fetch might return instead


# --------------------------------------------------------------- doubles


class DteWindowFakeProvider(DteWindowOptionChainProvider):
    """A MarketDataProvider double that DOES implement the
    provider-neutral `DteWindowOptionChainProvider` capability
    (PAPER_TRADING_V1.5.6), so confirmation's V1.5.8 preferred fetch path
    is actually exercised. `old_chain` simulates what the pre-V1.5.8
    nearest-N `get_option_chain` call would have returned, independent of
    `window_chain` -- letting tests prove the new exact-window path
    succeeds in cases the old path would not have, and recording every
    window request so a test can assert the exact (min_dte, max_dte) it
    was called with."""

    def __init__(self, *, window_chain: OptionChain | None = None, old_chain: OptionChain | None = None, raises: Exception | None = None):
        self._window_chain = window_chain
        self._old_chain = old_chain if old_chain is not None else window_chain
        self._raises = raises
        self.window_calls: list[tuple[str, int, int, date]] = []
        self.old_calls = 0

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date,
        diagnostics: DteWindowSelectionDiagnostics | None = None,
    ) -> OptionChain:
        if self._raises is not None:
            raise self._raises
        self.window_calls.append((symbol, min_dte, max_dte, as_of))
        return self._window_chain

    async def get_option_chain(self, symbol: str) -> OptionChain:
        self.old_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._old_chain

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self._window_chain.underlying


class PlainFakeProvider:
    """A MarketDataProvider double that does NOT implement
    DteWindowOptionChainProvider -- proves confirmation falls back to the
    pre-V1.5.8 `get_option_chain` call unchanged for such a provider."""

    def __init__(self, chain: OptionChain):
        self._chain = chain
        self.calls = 0

    async def get_option_chain(self, symbol: str) -> OptionChain:
        self.calls += 1
        return self._chain

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self._chain.underlying


# ------------------------------------------------------------- fixtures


def _underlying(px: float = 10.0, as_of: datetime = NOW) -> UnderlyingQuote:
    return UnderlyingQuote(symbol="SPY", bid=px - 0.05, ask=px + 0.05, last=px, volume=1_000_000, timestamp=as_of, source="test")


def _contract(
    *, strike: float, right: OptionRight, expiration: date = EXPIRATION, underlying: str = "SPY",
    bid: float = 0.48, ask: float = 0.52, underlying_px: float = 10.0, as_of: datetime = NOW,
    option_symbol: str | None = None,
) -> OptionContract:
    return OptionContract(
        option_symbol=option_symbol or build_occ_symbol(underlying, expiration, right, strike),
        underlying=underlying, strike=strike, expiration=expiration, right=right,
        bid=bid, ask=ask, last=(bid + ask) / 2, volume=500, open_interest=1000,
        delta=-0.2, iv=0.22, underlying_price=underlying_px, timestamp=as_of, source="test",
    )


def _csp_proposal(*, proposal_id: str = "cand-csp", strike: float = 5.0, expiration: date = EXPIRATION) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id, timestamp=NOW, ticker="SPY", strategy=StrategyType.CASH_SECURED_PUT,
        market_regime="normal", expiration=expiration, legs=[OptionLeg(right=SchemaOptionRight.PUT, strike=strike, side=LegSide.SELL)],
        direction=TradeDirection.NEUTRAL, contracts_requested=1, target_entry=0.5, profit_target=0.5,
        management_dte=21, thesis="t", risk_thesis="rt", confidence=Conviction.MEDIUM, data_sources=["test"],
        data_timestamp=NOW, invalidation_conditions=["x"],
    )


def _pcs_proposal(*, proposal_id: str = "cand-pcs", short_strike: float = 5.0, long_strike: float = 4.0, expiration: date = EXPIRATION) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id, timestamp=NOW, ticker="SPY", strategy=StrategyType.PUT_CREDIT_SPREAD,
        market_regime="normal", expiration=expiration,
        legs=[
            OptionLeg(right=SchemaOptionRight.PUT, strike=short_strike, side=LegSide.SELL),
            OptionLeg(right=SchemaOptionRight.PUT, strike=long_strike, side=LegSide.BUY),
        ],
        direction=TradeDirection.NEUTRAL, contracts_requested=1, target_entry=0.2, profit_target=0.5,
        management_dte=21, thesis="t", risk_thesis="rt", confidence=Conviction.MEDIUM, data_sources=["test"],
        data_timestamp=NOW, invalidation_conditions=["x"],
    )


def _iron_condor_proposal(*, proposal_id: str = "cand-ic", expiration: date = EXPIRATION) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id, timestamp=NOW, ticker="SPY", strategy=StrategyType.SHORT_IRON_CONDOR,
        market_regime="normal", expiration=expiration,
        legs=[
            OptionLeg(right=SchemaOptionRight.PUT, strike=2.0, side=LegSide.BUY),
            OptionLeg(right=SchemaOptionRight.PUT, strike=3.0, side=LegSide.SELL),
            OptionLeg(right=SchemaOptionRight.CALL, strike=6.0, side=LegSide.SELL),
            OptionLeg(right=SchemaOptionRight.CALL, strike=7.0, side=LegSide.BUY),
        ],
        direction=TradeDirection.NEUTRAL, contracts_requested=1, target_entry=0.3, profit_target=0.5,
        management_dte=21, thesis="t", risk_thesis="rt", confidence=Conviction.MEDIUM, data_sources=["test"],
        data_timestamp=NOW, invalidation_conditions=["x"],
    )


def _iron_condor_contracts() -> list[OptionContract]:
    """Differentiated per-strike pricing so the short (closer-to-money)
    legs net MORE premium than the long (farther) wings -- a short iron
    condor is a net-credit structure, and `resolve_credit` raises
    UndefinedEconomicsError on a non-positive net credit."""
    return [
        _contract(strike=2.0, right=OptionRight.PUT, bid=0.095, ask=0.105),  # long put wing (cheap)
        _contract(strike=3.0, right=OptionRight.PUT, bid=0.285, ask=0.315),  # short put (more expensive)
        _contract(strike=6.0, right=OptionRight.CALL, bid=0.285, ask=0.315),  # short call (more expensive)
        _contract(strike=7.0, right=OptionRight.CALL, bid=0.095, ask=0.105),  # long call wing (cheap)
    ]


def _make_candidate(proposal: TradeProposal, chain: OptionChain, *, candidate_id: str | None = None) -> ReviewedCandidate:
    """Builds a fully Quant/Risk-evaluated AWAITING_HUMAN candidate from a
    proposal and the (synthetic, offline) chain that was "current" at scan
    time -- exactly how scripts/run_validation_cycle.py builds one, just
    without any real I/O."""
    portfolio = make_portfolio()
    qa = default_quant_stage(proposal, chain, portfolio, LIMITS, now=NOW)
    risk = evaluate_trade_proposal(proposal, portfolio, qa, chain, BROKER_CAPS, limits=LIMITS, now=NOW)
    return ReviewedCandidate(
        candidate_id=candidate_id or proposal.proposal_id, cohort_id="cohort-1", cycle_id="cycle-1", created_at=NOW,
        ttl_seconds=900, proposal=proposal, quantitative_analysis=qa, risk_decision=risk,
        entry_delta=-0.2, entry_iv=0.22, quote_timestamp=chain.timestamp, market_data_source=chain.source,
        portfolio_exposure_before_pct=0.0, portfolio_exposure_after_pct=0.01,
        sector_exposure_before_pct=0.0, sector_exposure_after_pct=0.01, management_policy_name="default",
    )


def _mid_fill_broker(account_id: str = "PAPER-1") -> PaperBroker:
    broker = PaperBroker(initial_cash=100_000.0, account_id=account_id, now=NOW, config=PaperBrokerConfig(fill_model=FillModel.MID))
    return broker


def _build_inputs(*, candidate: ReviewedCandidate, provider, now: datetime = NOW, **overrides) -> ConfirmCandidateInputs:
    review_store = overrides.pop("review_store", InMemoryCandidateReviewStore())
    review_store.save_candidate(candidate)
    portfolio_store = overrides.pop("portfolio_store", InMemoryPortfolioStore())
    account_id = overrides.pop("account_id", "PAPER-1")
    if portfolio_store.get(account_id) is None:
        portfolio_store.save(account_id, make_portfolio())
    broker = overrides.pop("broker", _mid_fill_broker(account_id))
    defaults = dict(
        candidate_id=candidate.candidate_id, now=now, market_data_provider=provider, review_store=review_store,
        validation_store=InMemoryValidationStore(), portfolio_store=portfolio_store,
        account_state_store=InMemoryPaperAccountStateStore(), paper_broker=broker, limits=LIMITS,
        automated_broker_capabilities=BROKER_CAPS, max_price_drift_pct=0.05, max_capital_required_drift_pct=0.05,
    )
    defaults.update(overrides)
    return ConfirmCandidateInputs(**defaults)


# ------------------------------------------------------------------ tests


@pytest.mark.asyncio
class TestExactExpirationFetchPathV158:
    async def test_dte_window_capable_provider_is_preferred_and_called_with_exact_single_day_window(self):
        """Item 1: the candidate's expiration (45 DTE) is beyond what a
        small nearest-N window would return (simulated via `old_chain`
        being empty-contracts), but the new exact-window path succeeds
        because it targets the exact expiration directly."""
        proposal = _csp_proposal()
        scan_time_chain = OptionChain(
            underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test",
        )
        candidate = _make_candidate(proposal, scan_time_chain)

        old_chain_missing_target = OptionChain(underlying=_underlying(), contracts=[], timestamp=NOW, source="test")
        fresh_window_chain = OptionChain(
            underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=fresh_window_chain, old_chain=old_chain_missing_target)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.CONFIRMED
        assert provider.old_calls == 0, "the nearest-N get_option_chain path must not be used when the exact-window capability exists"
        assert len(provider.window_calls) == 1
        symbol, min_dte, max_dte, as_of = provider.window_calls[0]
        expected_dte = (EXPIRATION - NOW.date()).days
        assert symbol == "SPY"
        assert min_dte == max_dte == expected_dte
        assert as_of == NOW.date()

    async def test_provider_without_window_capability_falls_back_to_get_option_chain_unchanged(self):
        """Behavior-equivalence boundary: a provider that never implemented
        DteWindowOptionChainProvider must see IDENTICAL behavior to
        pre-V1.5.8 -- a plain get_option_chain call, nothing more."""
        proposal = _csp_proposal()
        chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, chain)
        provider = PlainFakeProvider(chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.CONFIRMED
        assert provider.calls == 1

    async def test_exact_expiration_unavailable_fails_closed(self):
        """Item 2: the provider genuinely has nothing at the candidate's
        exact expiration (empty window) -- DATA_INSUFFICIENT, never a
        fallback to a nearby expiration."""
        proposal = _csp_proposal()
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        empty_window_chain = OptionChain(underlying=_underlying(), contracts=[], timestamp=NOW, source="test")
        provider = DteWindowFakeProvider(window_chain=empty_window_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        saved = inputs.review_store.get_candidate(candidate.candidate_id)
        assert saved.status == CandidateStatus.DATA_INSUFFICIENT
        assert "exact persisted expiration" in saved.resolution_reason
        assert await inputs.paper_broker.get_fills() == []

    async def test_expiration_mismatch_in_returned_chain_fails_closed(self):
        """Item 5: the refreshed chain has a contract at the right strike
        and right, but at a DIFFERENT expiration than persisted -- the
        explicit expiration check must catch this before any per-leg
        matching is even attempted."""
        proposal = _csp_proposal()
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        wrong_expiration_chain = OptionChain(
            underlying=_underlying(),
            contracts=[_contract(strike=5.0, right=OptionRight.PUT, expiration=WRONG_EXPIRATION)],
            timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=wrong_expiration_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        saved = inputs.review_store.get_candidate(candidate.candidate_id)
        assert "exact persisted expiration" in saved.resolution_reason
        assert await inputs.paper_broker.get_fills() == []

    async def test_persisted_strike_missing_at_exact_expiration_fails_closed(self):
        """Item 3: the exact expiration IS present, but no contract at the
        candidate's persisted strike exists within it."""
        proposal = _csp_proposal(strike=5.0)
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        wrong_strike_chain = OptionChain(
            underlying=_underlying(), contracts=[_contract(strike=6.0, right=OptionRight.PUT)], timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=wrong_strike_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_right_mismatch_at_exact_expiration_fails_closed(self):
        """Item 4: same strike and expiration, but the refreshed contract
        is a CALL where the candidate's persisted leg is a PUT."""
        proposal = _csp_proposal(strike=5.0)
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        wrong_right_chain = OptionChain(
            underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.CALL)], timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=wrong_right_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_underlying_mismatch_at_exact_expiration_fails_closed(self):
        """Item 6: a contract matching strike/expiration/right but for a
        DIFFERENT underlying ticker (the MD-003 cross-check, exercised at
        the confirmation boundary) must still fail closed -- the explicit
        expiration check alone cannot catch this since `expiration`
        matches; `_find_contract`'s (underlying, expiration, strike,
        right) match is what must."""
        proposal = _csp_proposal(strike=5.0)
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        wrong_underlying_chain = OptionChain(
            underlying=_underlying(),
            contracts=[_contract(strike=5.0, right=OptionRight.PUT, underlying="QQQ", option_symbol=build_occ_symbol("QQQ", EXPIRATION, OptionRight.PUT, 5.0))],
            timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=wrong_underlying_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_stale_exact_contract_fails_closed(self):
        """Item 8: the exact contract is found, but its own timestamp is
        older than max_market_data_age_minutes."""
        proposal = _csp_proposal(strike=5.0)
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        stale_ts = NOW - timedelta(minutes=LIMITS.max_market_data_age_minutes + 5)
        stale_chain = OptionChain(
            underlying=_underlying(as_of=stale_ts),
            contracts=[_contract(strike=5.0, right=OptionRight.PUT, as_of=stale_ts)],
            timestamp=stale_ts, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=stale_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_future_dated_contract_beyond_clock_skew_tolerance_fails_closed(self):
        """Item 9: a contract timestamped materially AHEAD of `now` is
        treated as invalid/untrustworthy, never as fresher-than-fresh."""
        proposal = _csp_proposal(strike=5.0)
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        future_ts = NOW + timedelta(minutes=5)
        future_chain = OptionChain(
            underlying=_underlying(as_of=future_ts),
            contracts=[_contract(strike=5.0, right=OptionRight.PUT, as_of=future_ts)],
            timestamp=future_ts, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=future_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_fetch_exception_from_exact_window_path_is_data_insufficient_never_a_crash(self):
        proposal = _csp_proposal()
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        provider = DteWindowFakeProvider(raises=RuntimeError("provider unavailable"))

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT


@pytest.mark.asyncio
class TestMultiLegAtomicityV158:
    async def test_two_leg_spread_with_one_leg_missing_fails_the_entire_confirmation(self):
        """Item 10: a PUT_CREDIT_SPREAD's long leg cannot be refreshed --
        the whole confirmation must fail, never a partial (single-leg)
        fill."""
        proposal = _pcs_proposal()
        scan_chain = OptionChain(
            underlying=_underlying(),
            contracts=[
                _contract(strike=5.0, right=OptionRight.PUT, bid=0.48, ask=0.52),  # short leg, higher premium
                _contract(strike=4.0, right=OptionRight.PUT, bid=0.18, ask=0.22),  # long leg, lower premium -> net credit
            ],
            timestamp=NOW, source="test",
        )
        candidate = _make_candidate(proposal, scan_chain)
        # Refresh only returns the short leg's contract -- the long leg
        # (strike=4.0) is absent.
        partial_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        provider = DteWindowFakeProvider(window_chain=partial_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []
        portfolio = inputs.portfolio_store.get("PAPER-1")
        assert len(portfolio.positions) == 0

    async def test_four_leg_iron_condor_with_one_leg_missing_fails_the_entire_confirmation(self):
        """Item 11: a SHORT_IRON_CONDOR missing exactly one of its four
        legs in the refresh must fail entirely -- confirmation never
        builds a 3-leg structure as a substitute."""
        proposal = _iron_condor_proposal()
        full_contracts = _iron_condor_contracts()
        scan_chain = OptionChain(underlying=_underlying(), contracts=full_contracts, timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        # Refresh is missing the long call wing (strike=7.0).
        partial_contracts = [c for c in full_contracts if c.strike != 7.0]
        partial_chain = OptionChain(underlying=_underlying(), contracts=partial_contracts, timestamp=NOW, source="test")
        provider = DteWindowFakeProvider(window_chain=partial_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert await inputs.paper_broker.get_fills() == []

    async def test_four_leg_iron_condor_with_all_legs_exact_and_fresh_confirms(self):
        """Positive control for item 11/12/13/17: when all four legs ARE
        exactly present and fresh, Quant and Risk both rerun successfully
        and the existing PaperBroker path still fills."""
        proposal = _iron_condor_proposal()
        full_contracts = _iron_condor_contracts()
        scan_chain = OptionChain(underlying=_underlying(), contracts=full_contracts, timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        fresh_chain = OptionChain(underlying=_underlying(), contracts=full_contracts, timestamp=NOW, source="test")
        provider = DteWindowFakeProvider(window_chain=fresh_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.CONFIRMED
        fills = await inputs.paper_broker.get_fills()
        assert len(fills) == 4  # one fill per leg of the 4-leg combo
        assert len({f.broker_order_id for f in fills}) == 1  # all four legs filled as ONE order
        portfolio = inputs.portfolio_store.get("PAPER-1")
        assert len(portfolio.positions) == 1


@pytest.mark.asyncio
class TestRiskAndDriftStillApplyAfterExactRefreshV158:
    async def test_risk_rejection_after_exact_refresh_blocks_the_fill(self):
        """Items 13/15: the Risk Engine reruns against the CURRENT
        portfolio even on the new exact-window fetch path, and a rejection
        still blocks the fill exactly as before."""
        proposal = _csp_proposal()
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        fresh_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT)], timestamp=NOW, source="test")
        provider = DteWindowFakeProvider(window_chain=fresh_chain)

        portfolio_store = InMemoryPortfolioStore()
        portfolio_store.save("PAPER-1", make_portfolio(halted=True, halt_reason="test halt"))

        inputs = _build_inputs(candidate=candidate, provider=provider, portfolio_store=portfolio_store)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.REJECTED_BY_RISK
        assert await inputs.paper_broker.get_fills() == []

    async def test_price_drift_beyond_tolerance_after_exact_refresh_still_reprices(self):
        """Item 14: exact contract identity does not mean stale pricing is
        accepted -- a moved price on the SAME exact contract still trips
        the existing drift tolerance."""
        proposal = _csp_proposal()
        scan_chain = OptionChain(underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT, bid=0.48, ask=0.52)], timestamp=NOW, source="test")
        candidate = _make_candidate(proposal, scan_chain)
        moved_chain = OptionChain(
            underlying=_underlying(), contracts=[_contract(strike=5.0, right=OptionRight.PUT, bid=0.30, ask=0.32)], timestamp=NOW, source="test",
        )
        provider = DteWindowFakeProvider(window_chain=moved_chain)

        inputs = _build_inputs(candidate=candidate, provider=provider)
        outcome = await confirm_candidate(inputs)

        assert outcome == ConfirmationOutcome.REPRICE_REQUIRED
        assert await inputs.paper_broker.get_fills() == []


class TestOptionSymbolIdentityDocumentationV158:
    def test_option_symbol_is_not_part_of_the_persisted_leg_identity_today(self):
        """Item 7: Part 1/4 of the V1.5.8 spec ask that option_symbol be
        verified "where it exists and is authoritative" on the persisted
        candidate. `OptionLeg` (the schema `TradeProposal.legs` is made
        of) carries no `option_symbol` field -- there is nothing stored on
        a candidate to cross-check a refreshed contract's option_symbol
        against. This is documented here rather than silently assumed:
        the persisted, verifiable leg identity is exactly
        (underlying via TradeProposal.ticker, TradeProposal.expiration,
        OptionLeg.strike, OptionLeg.right) -- the same four fields
        `_find_contract` already matches on."""
        assert "option_symbol" not in OptionLeg.model_fields
        assert set(OptionLeg.model_fields) == {"right", "strike", "side", "quantity_ratio"}


class TestConfirmCandidateScriptNonMutatingArgErrorV158:
    def test_wrong_argument_count_never_touches_any_store(self, monkeypatch, capsys):
        """Item 18: invoking the CLI with the wrong number of arguments
        (the `--help`-shaped / preflight case) must return before any
        store, broker, or provider is constructed -- confirmed here by
        patching sys.argv and asserting the short-circuit return, never
        importing/engaging any database."""
        import sys

        import scripts.confirm_candidate as confirm_candidate_script

        monkeypatch.setattr(sys, "argv", ["confirm_candidate.py"])
        exit_code = confirm_candidate_script.main()
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "Usage:" in out
