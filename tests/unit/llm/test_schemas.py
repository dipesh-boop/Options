"""Schema-level proof that malformed or execution-shaped LLM output
cannot be constructed as a trusted object in the first place.

TradeProposal itself gets deep, dedicated coverage in
test_trade_proposal.py (required fields, forbidden fields, leg/strategy
validation, market data freshness). This file covers the shared
`_StrictModel` behavior (extra="forbid", frozen) across the other
agent-output schemas, plus the `ensure_trade_proposal` boundary guard.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.llm.schemas import (
    AdversarialReview,
    Conviction,
    LegSide,
    OptionLeg,
    OptionRight,
    StrategyType,
    TradeDirection,
    TradeProposal,
    ensure_trade_proposal,
)


def _valid_proposal_kwargs(**overrides) -> dict:
    base = dict(
        proposal_id="prop-1",
        timestamp=datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc),
        ticker="AAPL",
        strategy=StrategyType.CASH_SECURED_PUT,
        market_regime="normal",
        expiration=date(2026, 2, 20),
        legs=[OptionLeg(right=OptionRight.PUT, strike=210.0, side=LegSide.SELL)],
        direction=TradeDirection.BULLISH,
        contracts_requested=2,
        target_entry=2.10,
        profit_target=0.5,
        management_dte=21,
        thesis="High IV percentile, liquid chain, no earnings before expiry.",
        risk_thesis="Assignment risk if the stock drops sharply before expiry.",
        confidence=Conviction.MEDIUM,
        data_sources=["ibkr_snapshot"],
        data_timestamp=datetime(2026, 1, 15, 14, 20, tzinfo=timezone.utc),
        invalidation_conditions=["Close below 205 on the daily chart"],
    )
    base.update(overrides)
    return base


class TestTradeProposalPositiveControl:
    def test_valid_proposal_parses(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        assert proposal.proposal_id == "prop-1"
        assert proposal.ticker == "AAPL"

    def test_ensure_trade_proposal_accepts_real_instance(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        assert ensure_trade_proposal(proposal) is proposal


class TestEnsureTradeProposalBoundaryGuard:
    """This is the guard every future execution-adjacent entry point must
    call. It must reject anything that isn't a genuine, already-validated
    TradeProposal instance."""

    def test_rejects_plain_dict_even_with_correct_shape(self):
        payload = _valid_proposal_kwargs()
        payload_dict = {
            **payload,
            "legs": [leg.model_dump() for leg in payload["legs"]],
        }
        with pytest.raises(TypeError):
            ensure_trade_proposal(payload_dict)

    def test_rejects_other_schema_instance(self):
        review = AdversarialReview(proposal_id="prop-1", critique="Too correlated with existing book.")
        with pytest.raises(TypeError):
            ensure_trade_proposal(review)

    def test_rejects_subclass_instance(self):
        class SneakyTradeProposal(TradeProposal):
            pass

        proposal = SneakyTradeProposal(**_valid_proposal_kwargs())
        with pytest.raises(TypeError):
            ensure_trade_proposal(proposal)

    def test_rejects_none_and_primitives(self):
        for bad in (None, "trade_proposal", 42, [1, 2, 3]):
            with pytest.raises(TypeError):
                ensure_trade_proposal(bad)


class TestModelsAreFrozen:
    def test_trade_proposal_is_immutable(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        with pytest.raises(ValidationError):
            proposal.contracts_requested = 99  # type: ignore[misc]

    def test_adversarial_review_is_immutable(self):
        review = AdversarialReview(proposal_id="prop-1", critique="Concentration risk.")
        with pytest.raises(ValidationError):
            review.do_not_advance = True  # type: ignore[misc]


class TestOtherSchemasRejectExtraFields:
    def test_adversarial_review_rejects_extra_field(self):
        with pytest.raises(ValidationError):
            AdversarialReview.model_validate(
                {"proposal_id": "prop-1", "critique": "weak thesis", "unexpected_field": True}
            )
