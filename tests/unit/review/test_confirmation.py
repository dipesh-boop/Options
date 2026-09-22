"""Tests for src.review.confirmation.confirm_candidate -- the ONLY
function in this codebase that may call PaperBroker.place_order for a new
position during the Review-Only PAPER_TRADING_V1.4.4 validation.
Every ConfirmationOutcome branch is exercised."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.portfolio.account_state import InMemoryPaperAccountStateStore, InMemoryPortfolioStore
from src.review.candidates import CandidateStatus, InMemoryCandidateReviewStore
from src.review.confirmation import ConfirmationOutcome, ConfirmCandidateInputs, confirm_candidate
from src.validation.session import InMemoryValidationStore
from tests.unit.review.conftest import BROKER_CAPS, LIMITS, NOW, make_candidate, make_chain, make_portfolio


class FakeProvider:
    """A stub MarketDataProvider -- returns whatever chain factory it was
    given, or raises, never touches a real network."""

    def __init__(self, chain_factory=None, raises: Exception | None = None):
        self._chain_factory = chain_factory or make_chain
        self._raises = raises

    async def get_option_chain(self, symbol):
        if self._raises is not None:
            raise self._raises
        return self._chain_factory()

    async def get_underlying_quote(self, symbol):
        return self._chain_factory().underlying


def _mid_fill_broker(account_id: str = "PAPER-1") -> PaperBroker:
    """MID fill model applies no liquidity-adjusted slippage, so a limit
    price built from the exact same quote's own mid always clears --
    isolates these tests to the confirm_candidate revalidation logic
    itself, not PaperBroker's separate, already-tested realistic-fill
    behavior (see tests/unit/brokers/test_paper_broker.py for that)."""
    broker = PaperBroker(initial_cash=100_000.0, account_id=account_id, now=NOW, config=PaperBrokerConfig(fill_model=FillModel.MID))
    broker.update_market_data(make_chain())
    return broker


def _build_inputs(*, skip_portfolio_bootstrap: bool = False, **overrides) -> ConfirmCandidateInputs:
    portfolio = overrides.pop("portfolio", make_portfolio())
    account_id = overrides.pop("account_id", "PAPER-1")
    broker = overrides.pop("broker", None)
    if broker is None:
        broker = _mid_fill_broker(account_id)

    portfolio_store = overrides.pop("portfolio_store", InMemoryPortfolioStore())
    if not skip_portfolio_bootstrap and portfolio_store.get(account_id) is None:
        portfolio_store.save(account_id, portfolio)

    defaults = dict(
        candidate_id="cand-1", now=NOW, market_data_provider=FakeProvider(),
        review_store=InMemoryCandidateReviewStore(), validation_store=InMemoryValidationStore(),
        portfolio_store=portfolio_store, account_state_store=InMemoryPaperAccountStateStore(),
        paper_broker=broker, limits=LIMITS, automated_broker_capabilities=BROKER_CAPS,
        max_price_drift_pct=0.05, max_capital_required_drift_pct=0.05,
    )
    defaults.update(overrides)
    return ConfirmCandidateInputs(**defaults)


@pytest.mark.asyncio
class TestConfirmCandidateOutcomes:
    async def test_not_found_for_an_unknown_candidate_id(self):
        inputs = _build_inputs(candidate_id="does-not-exist")
        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.NOT_FOUND

    async def test_already_resolved_refuses_before_touching_the_broker(self):
        review_store = InMemoryCandidateReviewStore()
        from dataclasses import replace

        candidate = replace(make_candidate(), status=CandidateStatus.CONFIRMED)
        review_store.save_candidate(candidate)
        inputs = _build_inputs(review_store=review_store)

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.ALREADY_RESOLVED
        assert await inputs.paper_broker.get_fills() == []

    async def test_expired_candidate_never_fills(self):
        review_store = InMemoryCandidateReviewStore()
        candidate = make_candidate(ttl_seconds=1)
        review_store.save_candidate(candidate)
        inputs = _build_inputs(review_store=review_store, now=NOW + timedelta(seconds=2))

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.EXPIRED
        assert review_store.get_candidate("cand-1").status == CandidateStatus.EXPIRED
        assert await inputs.paper_broker.get_fills() == []

    async def test_data_insufficient_on_market_data_fetch_failure(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        inputs = _build_inputs(review_store=review_store, market_data_provider=FakeProvider(raises=RuntimeError("boom")))

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT
        assert review_store.get_candidate("cand-1").status == CandidateStatus.DATA_INSUFFICIENT

    async def test_data_insufficient_when_no_current_portfolio_state_exists(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        empty_portfolio_store = InMemoryPortfolioStore()  # deliberately never saved into
        inputs = _build_inputs(review_store=review_store, portfolio_store=empty_portfolio_store, skip_portfolio_bootstrap=True)

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.DATA_INSUFFICIENT

    async def test_rejected_by_risk_when_the_current_portfolio_is_halted(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        halted_portfolio = make_portfolio(halted=True, halt_reason="test halt")
        inputs = _build_inputs(review_store=review_store, portfolio=halted_portfolio)

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.REJECTED_BY_RISK
        assert review_store.get_candidate("cand-1").status == CandidateStatus.REJECTED

    async def test_reprice_required_when_price_drifts_beyond_tolerance(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        # A materially different price level at confirm time, spread kept
        # tight (~6.5%, well under the 15% liquidity limit) so this
        # exercises the price-drift check specifically, not a Risk Engine
        # liquidity rejection.
        moved_chain = lambda: make_chain(bid=0.30, ask=0.32)  # noqa: E731
        inputs = _build_inputs(review_store=review_store, market_data_provider=FakeProvider(chain_factory=moved_chain))

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.REPRICE_REQUIRED
        assert review_store.get_candidate("cand-1").status == CandidateStatus.REPRICE_REQUIRED
        assert await inputs.paper_broker.get_fills() == []

    async def test_confirmed_fills_exactly_once_and_persists_state(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        portfolio_store = InMemoryPortfolioStore()
        account_state_store = InMemoryPaperAccountStateStore()
        broker = _mid_fill_broker()
        inputs = _build_inputs(
            review_store=review_store, portfolio_store=portfolio_store,
            account_state_store=account_state_store, broker=broker, account_id="PAPER-1",
        )

        outcome = await confirm_candidate(inputs)
        assert outcome == ConfirmationOutcome.CONFIRMED

        candidate = review_store.get_candidate("cand-1")
        assert candidate.status == CandidateStatus.CONFIRMED
        assert candidate.paper_order_id is not None

        fills = await broker.get_fills()
        assert len(fills) == 1

        updated_portfolio = portfolio_store.get("PAPER-1")
        assert len(updated_portfolio.positions) == 1
        assert account_state_store.get("PAPER-1") is not None

        opportunities = inputs.validation_store.opportunities()
        assert len(opportunities) == 1
        assert opportunities[0].proposal_id == "cand-1"

    async def test_confirming_twice_never_creates_two_positions(self):
        review_store = InMemoryCandidateReviewStore()
        review_store.save_candidate(make_candidate())
        portfolio_store = InMemoryPortfolioStore()
        broker = _mid_fill_broker()
        inputs = _build_inputs(review_store=review_store, portfolio_store=portfolio_store, broker=broker, account_id="PAPER-1")

        first = await confirm_candidate(inputs)
        second = await confirm_candidate(inputs)

        assert first == ConfirmationOutcome.CONFIRMED
        assert second == ConfirmationOutcome.ALREADY_RESOLVED
        fills = await broker.get_fills()
        assert len(fills) == 1
        assert len(portfolio_store.get("PAPER-1").positions) == 1
        assert len(inputs.validation_store.opportunities()) == 1
