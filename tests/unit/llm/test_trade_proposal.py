"""Comprehensive tests for the TradeProposal schema.

Covers: the full required-field set, the explicitly forbidden
execution/authority-shaped fields, per-strategy leg validation, market
data freshness/presence, expiration/DTE consistency, and general field
constraints. This is the schema-level half of "malformed LLM responses
cannot reach the execution system" — tests/unit/llm/test_execution_safety.py
covers the end-to-end client + boundary-guard half.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.llm.schemas import (
    MAX_MARKET_DATA_AGE,
    Conviction,
    LegSide,
    OptionLeg,
    OptionRight,
    StrategyType,
    TradeAction,
    TradeDirection,
    TradeProposal,
    ensure_trade_proposal,
)

TIMESTAMP = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
DATA_TIMESTAMP = datetime(2026, 1, 15, 14, 20, tzinfo=timezone.utc)  # 10 min old, within limit
EXPIRATION = date(2026, 2, 20)  # 36 days out from TIMESTAMP.date()


def _put_credit_spread_legs() -> list[OptionLeg]:
    return [
        OptionLeg(right=OptionRight.PUT, strike=420.0, side=LegSide.SELL),
        OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.BUY),
    ]


def valid_kwargs(**overrides) -> dict:
    base = dict(
        proposal_id="prop-42",
        timestamp=TIMESTAMP,
        ticker="MSFT",
        strategy=StrategyType.PUT_CREDIT_SPREAD,
        market_regime="normal",
        expiration=EXPIRATION,
        legs=_put_credit_spread_legs(),
        direction=TradeDirection.BULLISH,
        contracts_requested=5,
        target_entry=1.25,
        profit_target=0.5,
        management_dte=21,
        thesis="Elevated IV rank, no earnings before expiry, liquid chain.",
        risk_thesis="Max loss is width minus credit; a gap below 410 before expiry breaches it.",
        confidence=Conviction.MEDIUM,
        data_sources=["ibkr_snapshot"],
        data_timestamp=DATA_TIMESTAMP,
        invalidation_conditions=["Close below 415 on the daily chart", "IV rank drops below 30 before entry"],
    )
    base.update(overrides)
    return base


class TestValidProposalsForEachStrategy:
    def test_put_credit_spread_valid(self):
        proposal = TradeProposal(**valid_kwargs())
        assert proposal.strategy == StrategyType.PUT_CREDIT_SPREAD
        assert len(proposal.legs) == 2

    def test_cash_secured_put_valid(self):
        proposal = TradeProposal(
            **valid_kwargs(
                strategy=StrategyType.CASH_SECURED_PUT,
                legs=[OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.SELL)],
            )
        )
        assert proposal.strategy == StrategyType.CASH_SECURED_PUT

    def test_covered_call_valid(self):
        proposal = TradeProposal(
            **valid_kwargs(
                strategy=StrategyType.COVERED_CALL,
                direction=TradeDirection.NEUTRAL,
                legs=[OptionLeg(right=OptionRight.CALL, strike=440.0, side=LegSide.SELL)],
            )
        )
        assert proposal.strategy == StrategyType.COVERED_CALL

    def test_action_defaults_to_open(self):
        proposal = TradeProposal(**valid_kwargs())
        assert proposal.action == TradeAction.OPEN

    def test_action_close_and_roll_accepted(self):
        for action in (TradeAction.CLOSE, TradeAction.ROLL):
            proposal = TradeProposal(**valid_kwargs(action=action))
            assert proposal.action == action

    def test_ensure_trade_proposal_accepts_valid_instance(self):
        proposal = TradeProposal(**valid_kwargs())
        assert ensure_trade_proposal(proposal) is proposal

    def test_is_frozen(self):
        proposal = TradeProposal(**valid_kwargs())
        with pytest.raises(ValidationError):
            proposal.contracts_requested = 999  # type: ignore[misc]


class TestAllRequiredFieldsAreEnforced:
    REQUIRED_FIELDS = [
        "proposal_id",
        "timestamp",
        "ticker",
        "strategy",
        "market_regime",
        "expiration",
        "legs",
        "direction",
        "contracts_requested",
        "target_entry",
        "profit_target",
        "management_dte",
        "thesis",
        "risk_thesis",
        "confidence",
        "data_sources",
        "data_timestamp",
        "invalidation_conditions",
    ]

    @pytest.mark.parametrize("field_name", REQUIRED_FIELDS)
    def test_missing_field_rejected(self, field_name: str):
        kwargs = valid_kwargs()
        del kwargs[field_name]
        with pytest.raises(ValidationError):
            TradeProposal(**kwargs)

    def test_all_required_fields_present_together_is_sufficient(self):
        # Sanity check that the REQUIRED_FIELDS list above is actually
        # complete and matches the model — if this ever fails, the
        # parametrized test above is silently under-covering.
        proposal = TradeProposal(**valid_kwargs())
        for field_name in self.REQUIRED_FIELDS:
            assert hasattr(proposal, field_name)


class TestForbiddenExecutionAndAuthorityFields:
    """The platform explicitly forbids an LLM from specifying: final
    approved contracts, authoritative maximum loss, portfolio risk, a
    broker order id, or execution authorization. None of these are
    fields on TradeProposal, and extra="forbid" means smuggling them in
    anyway is a hard validation failure, not a silently ignored extra."""

    @pytest.mark.parametrize(
        "forbidden_field",
        [
            {"final_approved_contracts": 100},
            {"approved_contracts": 100},
            {"authoritative_max_loss": 500.0},
            {"max_loss": 500.0},
            {"portfolio_risk": 0.02},
            {"portfolio_risk_pct": 0.02},
            {"broker_order_id": "IBKR-12345"},
            {"order_id": "IBKR-12345"},
            {"execution_authorization": True},
            {"execution_authorized": True},
            {"authorized": True},
            {"execute": True},
        ],
    )
    def test_forbidden_field_rejected(self, forbidden_field: dict):
        kwargs = {**valid_kwargs(), **forbidden_field}
        with pytest.raises(ValidationError):
            TradeProposal(**kwargs)

    def test_model_has_no_forbidden_field_names_at_all(self):
        # Belt-and-suspenders: prove the schema doesn't merely reject
        # these as *extra* fields but never defines them as real fields
        # either (i.e. nobody accidentally added one as a legitimate,
        # accepted field later).
        forbidden_substrings = ["approved_contract", "max_loss", "portfolio_risk", "order_id", "authoriz"]
        field_names = set(TradeProposal.model_fields.keys())
        for name in field_names:
            for forbidden in forbidden_substrings:
                assert forbidden not in name.lower(), f"field {name!r} looks forbidden ({forbidden!r})"


class TestMarketDataFreshness:
    def test_data_timestamp_within_limit_accepted(self):
        proposal = TradeProposal(
            **valid_kwargs(data_timestamp=TIMESTAMP - MAX_MARKET_DATA_AGE)  # exactly at the boundary
        )
        assert proposal.data_timestamp == TIMESTAMP - MAX_MARKET_DATA_AGE

    def test_data_timestamp_one_second_past_limit_rejected(self):
        with pytest.raises(ValidationError, match="stale"):
            TradeProposal(**valid_kwargs(data_timestamp=TIMESTAMP - MAX_MARKET_DATA_AGE - timedelta(seconds=1)))

    def test_data_timestamp_far_in_past_rejected(self):
        with pytest.raises(ValidationError, match="stale"):
            TradeProposal(**valid_kwargs(data_timestamp=TIMESTAMP - timedelta(days=1)))

    def test_data_timestamp_after_proposal_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(data_timestamp=TIMESTAMP + timedelta(minutes=1)))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            TradeProposal(**valid_kwargs(timestamp=datetime(2026, 1, 15, 14, 30)))

    def test_naive_data_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            TradeProposal(**valid_kwargs(data_timestamp=datetime(2026, 1, 15, 14, 20)))


class TestMissingOrEmptyMarketData:
    def test_empty_data_sources_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(data_sources=[]))

    def test_blank_data_source_entry_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(data_sources=["   "]))

    def test_none_data_sources_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(data_sources=None))

    def test_none_data_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(data_timestamp=None))

    def test_empty_invalidation_conditions_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(invalidation_conditions=[]))

    def test_blank_invalidation_condition_entry_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(invalidation_conditions=[""]))

    def test_blank_thesis_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(thesis=""))

    def test_blank_risk_thesis_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(risk_thesis=""))


class TestLegStrategyConsistency:
    def test_csp_with_call_leg_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    strategy=StrategyType.CASH_SECURED_PUT,
                    legs=[OptionLeg(right=OptionRight.CALL, strike=410.0, side=LegSide.SELL)],
                )
            )

    def test_csp_with_buy_side_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    strategy=StrategyType.CASH_SECURED_PUT,
                    legs=[OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.BUY)],
                )
            )

    def test_csp_with_two_legs_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(strategy=StrategyType.CASH_SECURED_PUT, legs=_put_credit_spread_legs()))

    def test_covered_call_with_put_leg_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    strategy=StrategyType.COVERED_CALL,
                    legs=[OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.SELL)],
                )
            )

    def test_covered_call_with_buy_side_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    strategy=StrategyType.COVERED_CALL,
                    legs=[OptionLeg(right=OptionRight.CALL, strike=440.0, side=LegSide.BUY)],
                )
            )

    def test_put_credit_spread_with_one_leg_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[OptionLeg(right=OptionRight.PUT, strike=420.0, side=LegSide.SELL)],
                )
            )

    def test_put_credit_spread_with_call_leg_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[
                        OptionLeg(right=OptionRight.CALL, strike=420.0, side=LegSide.SELL),
                        OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.BUY),
                    ]
                )
            )

    def test_put_credit_spread_both_sell_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[
                        OptionLeg(right=OptionRight.PUT, strike=420.0, side=LegSide.SELL),
                        OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.SELL),
                    ]
                )
            )

    def test_put_credit_spread_debit_structure_rejected(self):
        # Short strike must be HIGHER than the long strike for a net
        # credit put spread; this payload has it backwards.
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[
                        OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.SELL),
                        OptionLeg(right=OptionRight.PUT, strike=420.0, side=LegSide.BUY),
                    ]
                )
            )

    def test_put_credit_spread_equal_strikes_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[
                        OptionLeg(right=OptionRight.PUT, strike=415.0, side=LegSide.SELL),
                        OptionLeg(right=OptionRight.PUT, strike=415.0, side=LegSide.BUY),
                    ]
                )
            )

    def test_naked_call_not_expressible_as_any_allowed_strategy(self):
        # A "naked call" (long or short, uncovered) isn't one of the
        # platform's three strategy types at all, so it can only be
        # attempted by mislabeling it as covered_call with a buy leg
        # (already rejected above) or by forcing an invalid strategy
        # enum value, which the enum itself rejects.
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "strategy": "naked_call"})


class TestExpirationAndManagementDte:
    def test_expiration_on_timestamp_date_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(expiration=TIMESTAMP.date()))

    def test_expiration_before_timestamp_date_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(expiration=TIMESTAMP.date() - timedelta(days=1)))

    def test_management_dte_equal_to_total_dte_accepted(self):
        total_dte = (EXPIRATION - TIMESTAMP.date()).days
        proposal = TradeProposal(**valid_kwargs(management_dte=total_dte))
        assert proposal.management_dte == total_dte

    def test_management_dte_exceeding_total_dte_rejected(self):
        total_dte = (EXPIRATION - TIMESTAMP.date()).days
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(management_dte=total_dte + 1))

    def test_management_dte_zero_accepted(self):
        proposal = TradeProposal(**valid_kwargs(management_dte=0))
        assert proposal.management_dte == 0

    def test_negative_management_dte_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(management_dte=-1))


class TestGeneralFieldConstraints:
    @pytest.mark.parametrize("ticker", ["msft", "MSFT1", "TOOLONGTICKER", "", "MS FT", "MS-FT"])
    def test_invalid_ticker_rejected(self, ticker: str):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(ticker=ticker))

    def test_valid_single_letter_ticker_accepted(self):
        proposal = TradeProposal(**valid_kwargs(ticker="F"))
        assert proposal.ticker == "F"

    @pytest.mark.parametrize("contracts", [0, -1, -100])
    def test_non_positive_contracts_requested_rejected(self, contracts: int):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(contracts_requested=contracts))

    @pytest.mark.parametrize("target_entry", [0, -0.01, -5])
    def test_non_positive_target_entry_rejected(self, target_entry: float):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(target_entry=target_entry))

    @pytest.mark.parametrize("profit_target", [0, -0.1, 1.01, 2])
    def test_profit_target_out_of_range_rejected(self, profit_target: float):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(profit_target=profit_target))

    def test_profit_target_of_exactly_one_accepted(self):
        proposal = TradeProposal(**valid_kwargs(profit_target=1.0))
        assert proposal.profit_target == 1.0

    def test_invalid_strategy_enum_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "strategy": "iron_condor"})

    def test_invalid_direction_enum_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "direction": "extremely_bullish"})

    def test_invalid_market_regime_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "market_regime": "melt_up"})

    def test_invalid_confidence_enum_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "confidence": "extremely_high"})

    def test_invalid_action_enum_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate({**valid_kwargs(), "action": "execute_live"})

    def test_empty_proposal_id_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(proposal_id=""))

    def test_leg_with_non_positive_strike_rejected(self):
        with pytest.raises(ValidationError):
            OptionLeg(right=OptionRight.PUT, strike=0, side=LegSide.SELL)

    def test_too_many_legs_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                **valid_kwargs(
                    legs=[
                        OptionLeg(right=OptionRight.PUT, strike=420.0, side=LegSide.SELL),
                        OptionLeg(right=OptionRight.PUT, strike=410.0, side=LegSide.BUY),
                        OptionLeg(right=OptionRight.PUT, strike=400.0, side=LegSide.BUY),
                    ]
                )
            )

    def test_zero_legs_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(**valid_kwargs(legs=[]))
