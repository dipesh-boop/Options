"""Extensive bypass-attempt suite: every item Step 9 explicitly named,
plus the fail-closed catch-all. Each test starts from a known-good
baseline scenario (tests/unit/risk/conftest.py) and introduces exactly
one violation, then asserts the engine catches it — never APPROVE, and
the reason code names the specific thing that was tried.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataOptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import LegSide, OptionLeg
from src.llm.schemas import OptionRight as ProposalOptionRight
from src.risk.engine import evaluate_trade_proposal
from src.risk.portfolio_risk import PortfolioPosition, PortfolioPositionLeg, UnderlyingHolding
from src.risk.reason_codes import ReasonCode, RiskDecision
from src.risk.trade_risk import resolve_leg_contracts
from tests.unit.risk.conftest import (
    EXPIRATION,
    MD_TS,
    NOW,
    build_approved_covered_call_scenario,
    build_approved_csp_scenario,
    build_approved_pcs_scenario,
    fidelity_capabilities,
    make_contract,
    make_portfolio,
    make_underlying,
    pcs_proposal,
    quantitative_analysis_from,
)


def _evaluate(scenario, **overrides):
    kwargs = dict(
        proposal_obj=scenario.proposal,
        portfolio=scenario.portfolio,
        quantitative_analysis=scenario.quantitative_analysis,
        market_data=scenario.market_data,
        broker_capabilities=scenario.broker_capabilities,
        limits=scenario.limits,
        now=NOW,
    )
    kwargs.update(overrides)
    return evaluate_trade_proposal(**kwargs)


class TestOversizedPositions:
    def test_huge_request_is_capped_never_exceeding_requested_and_never_exceeding_risk_budget(self):
        scenario = build_approved_pcs_scenario()
        oversized_proposal = pcs_proposal(contracts_requested=500, proposal_id=scenario.proposal.proposal_id)
        resolved = resolve_leg_contracts(
            oversized_proposal, scenario.market_data, as_of=NOW, max_age_minutes=scenario.limits.max_market_data_age_minutes
        )
        qa = quantitative_analysis_from(oversized_proposal, resolved, scenario.portfolio, scenario.limits)

        result = _evaluate(scenario, proposal_obj=oversized_proposal, quantitative_analysis=qa)

        assert result.decision == RiskDecision.RESIZE
        assert result.reason_codes == [ReasonCode.RESIZED_POSITION_RISK]
        assert result.approved_contracts is not None
        assert result.approved_contracts < 500
        # 1% of $100,000 NAV / ~$425 max loss per contract ~= 2
        assert result.approved_contracts <= 3

    def test_a_request_that_cannot_be_sized_at_all_is_rejected_not_silently_zeroed_and_approved(self):
        scenario = build_approved_pcs_scenario(nav=1_000.0, cash=900.0, peak_equity=1_000.0)
        oversized_proposal = pcs_proposal(contracts_requested=500, proposal_id=scenario.proposal.proposal_id)
        resolved = resolve_leg_contracts(
            oversized_proposal, scenario.market_data, as_of=NOW, max_age_minutes=scenario.limits.max_market_data_age_minutes
        )
        qa = quantitative_analysis_from(oversized_proposal, resolved, scenario.portfolio, scenario.limits)

        result = _evaluate(scenario, proposal_obj=oversized_proposal, quantitative_analysis=qa)

        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_MAX_TRADE_RISK]
        assert result.approved_contracts is None


class TestCorrelatedPositions:
    def test_highly_correlated_new_position_is_rejected(self):
        scenario = build_approved_pcs_scenario()
        qqq_position = PortfolioPosition(
            position_id="pos-qqq-1",
            ticker="QQQ",
            sector="INDEX",
            strategy=scenario.proposal.strategy,
            expiration=EXPIRATION,
            legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=1.0)],
            contracts=1,
            capital_at_risk=500.0,
            max_loss=500.0,
            opened_at=NOW - timedelta(days=1),
        )
        # Nearly identical price paths => correlation ~1.0, far above the
        # configured 0.70 threshold.
        base_series = list(np.linspace(400.0, 420.0, 30))
        portfolio = scenario.portfolio.model_copy(
            update={
                "positions": [qqq_position],
                "price_history": {"SPY": base_series, "QQQ": [p * 1.5 for p in base_series]},
            }
        )
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_CORRELATION]

    def test_uncorrelated_or_unmeasured_existing_position_does_not_block(self):
        scenario = build_approved_pcs_scenario()
        qqq_position = PortfolioPosition(
            position_id="pos-qqq-1",
            ticker="QQQ",
            sector="INDEX",
            strategy=scenario.proposal.strategy,
            expiration=EXPIRATION,
            legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=1.0)],
            contracts=1,
            capital_at_risk=500.0,
            max_loss=500.0,
            opened_at=NOW - timedelta(days=1),
        )
        # No price_history supplied at all: the documented gap means this
        # is skipped, not fail-closed-rejected (see src.risk.correlation).
        portfolio = scenario.portfolio.model_copy(update={"positions": [qqq_position]})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)


class TestStaleQuotes:
    def test_stale_underlying_quote_rejected(self):
        scenario = build_approved_pcs_scenario()
        stale_underlying = make_underlying(timestamp=NOW - timedelta(hours=2))
        market_data = scenario.market_data.model_copy(update={"underlying": stale_underlying})
        result = _evaluate(scenario, market_data=market_data)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_STALE_DATA]

    def test_stale_contract_quote_rejected(self):
        scenario = build_approved_pcs_scenario()
        stale_contracts = [c.model_copy(update={"timestamp": NOW - timedelta(hours=2)}) for c in scenario.market_data.contracts]
        market_data = scenario.market_data.model_copy(update={"contracts": stale_contracts})
        result = _evaluate(scenario, market_data=market_data)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_STALE_DATA]


class TestFreshnessGateCannotBeBypassedRegressionMD001:
    """MD-001: `evaluate_trade_proposal` used to accept `now=None` (its
    default) and silently fall back to `proposal.timestamp` -- the
    proposal's own self-reported time, never independently verified.
    Since market data and the proposal are normally stamped moments
    apart, that fallback made the freshness gate report "fresh"
    regardless of how much real time had actually elapsed. `now` is now
    a required keyword-only argument with no fallback at all."""

    def test_now_is_a_required_argument_not_optional(self):
        scenario = build_approved_pcs_scenario()
        with pytest.raises(TypeError):
            evaluate_trade_proposal(
                scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
                scenario.market_data, scenario.broker_capabilities, limits=scenario.limits,
            )

    def test_real_elapsed_time_is_caught_even_though_proposal_timestamp_is_unchanged(self):
        """The exact MD-001 exploit shape: the proposal's own timestamp
        still says "just now" (an attacker/caller never has to touch
        it), but real time has moved on since the market data was
        fetched -- the freshness gate must catch this using the
        independently-supplied wall clock, not the proposal's word for
        it."""
        scenario = build_approved_pcs_scenario()
        much_later = NOW + timedelta(hours=2)
        result = _evaluate(scenario, now=much_later)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_STALE_DATA]

    def test_genuinely_fresh_as_of_the_supplied_now_still_approves(self):
        scenario = build_approved_pcs_scenario()
        result = _evaluate(scenario, now=NOW)
        assert result.decision == RiskDecision.APPROVE


class TestMissingQuotes:
    def test_missing_leg_contract_is_rejected(self):
        scenario = build_approved_pcs_scenario()
        # Drop the long-leg contract entirely — the short leg alone
        # cannot price a put credit spread.
        only_short = [c for c in scenario.market_data.contracts if c.strike == 620.0]
        market_data = scenario.market_data.model_copy(update={"contracts": only_short})
        result = _evaluate(scenario, market_data=market_data)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_INVALID_CONTRACT]


class TestMissingMaxLoss:
    def test_missing_implied_volatility_blocks_approval(self):
        scenario = build_approved_pcs_scenario()
        no_iv_contracts = [c.model_copy(update={"iv": None}) for c in scenario.market_data.contracts]
        market_data = scenario.market_data.model_copy(update={"contracts": no_iv_contracts})
        result = _evaluate(scenario, market_data=market_data)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_UNDEFINED_MAX_LOSS]


class TestExcessiveDrawdown:
    def test_drawdown_past_halt_threshold_halts_regardless_of_trade_quality(self):
        scenario = build_approved_pcs_scenario(nav=80_000.0, cash=70_000.0, peak_equity=100_000.0)  # 20% drawdown
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.HALT
        assert result.reason_codes == [ReasonCode.HALT_PORTFOLIO_DRAWDOWN]

    def test_manual_kill_switch_halts_regardless_of_trade_quality(self):
        scenario = build_approved_pcs_scenario()
        portfolio = scenario.portfolio.model_copy(update={"halted": True, "halt_reason": "operator emergency stop"})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.HALT
        assert result.reason_codes == [ReasonCode.HALT_MANUAL_KILL_SWITCH]

    def test_risk_reduction_zone_tightens_sizing(self):
        # 11% drawdown: inside [risk_reduction_pct=10%, halt_pct=15%).
        scenario = build_approved_pcs_scenario(nav=89_000.0, cash=80_000.0, peak_equity=100_000.0)
        oversized_proposal = pcs_proposal(contracts_requested=10, proposal_id=scenario.proposal.proposal_id)
        resolved = resolve_leg_contracts(
            oversized_proposal, scenario.market_data, as_of=NOW, max_age_minutes=scenario.limits.max_market_data_age_minutes
        )
        qa = quantitative_analysis_from(oversized_proposal, resolved, scenario.portfolio, scenario.limits)
        result = _evaluate(scenario, proposal_obj=oversized_proposal, quantitative_analysis=qa)
        assert result.decision in (RiskDecision.RESIZE, RiskDecision.REJECT)
        if result.decision == RiskDecision.RESIZE:
            # Half the normal target-risk budget in this zone: roughly
            # half of what an identical request would get at full NAV.
            assert result.approved_contracts <= 2


class TestInsufficientCash:
    def test_capital_required_exceeds_cash_on_hand(self):
        scenario = build_approved_pcs_scenario(cash=100.0)
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes in ([ReasonCode.REJECT_BUYING_POWER], [ReasonCode.REJECT_INSUFFICIENT_CASH])

    def test_cash_reserve_minimum_breached_even_when_capital_is_technically_available(self):
        # Enough cash to cover the trade's capital requirement, but too
        # little left over afterward to satisfy the minimum reserve.
        scenario = build_approved_pcs_scenario(nav=100_000.0, cash=1_000.0, peak_equity=100_000.0)
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes[0] in (ReasonCode.REJECT_INSUFFICIENT_CASH, ReasonCode.REJECT_BUYING_POWER)


class TestExcessiveConcentration:
    def test_sector_concentration_breach(self):
        scenario = build_approved_pcs_scenario()
        existing = PortfolioPosition(
            position_id="pos-existing-1",
            ticker="QQQ",
            sector="INDEX",
            strategy=scenario.proposal.strategy,
            expiration=EXPIRATION,
            legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=1.0)],
            contracts=1,
            capital_at_risk=24_800.0,  # already 24.8% of $100k NAV in the same INDEX sector
            max_loss=24_800.0,
            opened_at=NOW - timedelta(days=1),
        )
        sector_by_ticker = dict(scenario.portfolio.sector_by_ticker)
        sector_by_ticker["QQQ"] = "INDEX"
        portfolio = scenario.portfolio.model_copy(update={"positions": [existing], "sector_by_ticker": sector_by_ticker})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_SECTOR_CONCENTRATION]

    def test_underlying_concentration_breach(self):
        scenario = build_approved_pcs_scenario()
        existing = PortfolioPosition(
            position_id="pos-existing-2",
            ticker="SPY",
            sector="INDEX",
            strategy=scenario.proposal.strategy,
            expiration=EXPIRATION,
            legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=1.0)],
            contracts=1,
            capital_at_risk=9_800.0,  # already 9.8% of $100k NAV on SPY alone
            max_loss=9_800.0,
            opened_at=NOW - timedelta(days=1),
        )
        portfolio = scenario.portfolio.model_copy(update={"positions": [existing]})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_UNDERLYING_CONCENTRATION]


class TestDuplicatePosition:
    def test_identical_open_position_blocks_a_second_identical_proposal(self):
        scenario = build_approved_pcs_scenario()
        duplicate = PortfolioPosition(
            position_id="pos-dup-1",
            ticker="SPY",
            sector="INDEX",
            strategy=scenario.proposal.strategy,
            expiration=EXPIRATION,
            legs=[
                PortfolioPositionLeg(right="P", side="sell", strike=620.0, entry_price=0.75),
                PortfolioPositionLeg(right="P", side="buy", strike=615.0, entry_price=0.20),
            ],
            contracts=2,
            capital_at_risk=850.0,
            max_loss=850.0,
            opened_at=NOW - timedelta(days=1),
        )
        portfolio = scenario.portfolio.model_copy(update={"positions": [duplicate]})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_DUPLICATE_POSITION]


class TestInvalidOptionContract:
    def test_proposal_leg_strike_with_no_matching_market_contract_at_all(self):
        scenario = build_approved_pcs_scenario()
        proposal = scenario.proposal.model_copy(
            update={
                "legs": [
                    OptionLeg(right=ProposalOptionRight.PUT, strike=999.0, side=LegSide.SELL),
                    OptionLeg(right=ProposalOptionRight.PUT, strike=990.0, side=LegSide.BUY),
                ]
            }
        )
        result = _evaluate(scenario, proposal_obj=proposal)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_INVALID_CONTRACT]


class TestUnknownFidelityCapability:
    def test_broker_not_configured_at_all(self):
        scenario = build_approved_pcs_scenario()
        result = _evaluate(scenario, broker_capabilities=None)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_ACCOUNT_CAPABILITY]

    def test_options_not_enabled_on_the_account(self):
        scenario = build_approved_pcs_scenario()
        caps = fidelity_capabilities(options_enabled=False)
        result = _evaluate(scenario, broker_capabilities=caps)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_ACCOUNT_CAPABILITY]


class TestUnsupportedStrategy:
    def test_strategy_not_in_the_brokers_allowed_list(self):
        scenario = build_approved_pcs_scenario()
        caps = fidelity_capabilities(allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL"])  # PCS excluded
        result = _evaluate(scenario, broker_capabilities=caps)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_ACCOUNT_CAPABILITY]


class TestMissingCollateral:
    def test_covered_call_without_owning_the_underlying_shares(self):
        scenario = build_approved_covered_call_scenario()
        portfolio = scenario.portfolio.model_copy(update={"underlying_holdings": {}})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_MISSING_COLLATERAL]

    def test_cash_secured_put_without_enough_cash_to_secure_it(self):
        scenario = build_approved_csp_scenario(cash=100.0)
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.REJECT
        # Insufficient cash for collateral surfaces first, before the
        # general buying-power check runs.
        assert result.reason_codes == [ReasonCode.REJECT_MISSING_COLLATERAL]


class TestBrokerAccountMismatch:
    def test_portfolio_account_alias_does_not_match_broker_capabilities(self):
        scenario = build_approved_pcs_scenario()
        portfolio = scenario.portfolio.model_copy(update={"account_alias": "SOME_OTHER_ACCOUNT"})
        result = _evaluate(scenario, portfolio=portfolio)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_BROKER_ACCOUNT_MISMATCH]


class TestMalformedTradeProposal:
    def test_a_plain_dict_shaped_like_a_proposal_is_rejected(self):
        scenario = build_approved_pcs_scenario()
        fake = {"ticker": "SPY", "strategy": "put_credit_spread"}
        result = _evaluate(scenario, proposal_obj=fake)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_MALFORMED_PROPOSAL]

    def test_a_subclass_of_trade_proposal_is_rejected_too(self):
        from src.llm.schemas import TradeProposal

        class SneakyTradeProposal(TradeProposal):
            pass

        scenario = build_approved_pcs_scenario()
        sneaky = SneakyTradeProposal(**scenario.proposal.model_dump())
        result = _evaluate(scenario, proposal_obj=sneaky)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_MALFORMED_PROPOSAL]

    def test_market_data_for_a_different_ticker_than_the_proposal(self):
        scenario = build_approved_pcs_scenario()
        other_underlying = make_underlying(symbol="QQQ")
        market_data = scenario.market_data.model_copy(update={"underlying": other_underlying})
        result = _evaluate(scenario, market_data=market_data)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_MALFORMED_PROPOSAL]


class TestZeroNegativeNaNInfiniteValues:
    """These inputs never even reach evaluate_trade_proposal's decision
    logic: the Pydantic boundary types (Portfolio, QuantitativeAnalysis,
    TradeProposal) reject them at construction. That IS the fail-closed
    proof — there is no instance of these types carrying a zero/negative/
    NaN/infinite value for evaluate_trade_proposal to ever be called
    with."""

    def test_zero_nav_portfolio_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(nav=0.0)

    def test_negative_nav_portfolio_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(nav=-50_000.0)

    def test_negative_cash_portfolio_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(cash=-1.0)

    def test_nan_nav_portfolio_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(nav=math.nan)

    def test_infinite_nav_portfolio_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(nav=math.inf, cash=1.0, peak_equity=math.inf)

    def test_infinite_peak_equity_cannot_be_constructed(self):
        with pytest.raises(Exception):
            make_portfolio(peak_equity=math.inf)

    def test_nan_quantitative_analysis_field_cannot_be_constructed(self):
        from src.risk.trade_risk import QuantitativeAnalysis

        with pytest.raises(Exception):
            QuantitativeAnalysis(
                proposal_id="p", generated_at=MD_TS, max_profit=math.nan, max_loss=100.0, breakeven=600.0,
                capital_required=100.0, return_on_capital=0.1, annualized_roc=0.1, probability_of_profit=0.5,
                expected_value=10.0, net_delta=0.0, net_vega=0.0,
            )

    def test_infinite_quantitative_analysis_field_cannot_be_constructed(self):
        from src.risk.trade_risk import QuantitativeAnalysis

        with pytest.raises(Exception):
            QuantitativeAnalysis(
                proposal_id="p", generated_at=MD_TS, max_profit=100.0, max_loss=math.inf, breakeven=600.0,
                capital_required=100.0, return_on_capital=0.1, annualized_roc=0.1, probability_of_profit=0.5,
                expected_value=10.0, net_delta=0.0, net_vega=0.0,
            )

    def test_zero_contracts_requested_rejected_by_trade_proposal_schema(self):
        with pytest.raises(Exception):
            pcs_proposal(contracts_requested=0)

    def test_negative_strike_rejected_by_option_leg_schema(self):
        with pytest.raises(Exception):
            OptionLeg(right=ProposalOptionRight.PUT, strike=-620.0, side=LegSide.SELL)


class TestUnknownRiskFailsClosed:
    def test_an_unanticipated_exception_inside_evaluation_still_rejects(self, monkeypatch: pytest.MonkeyPatch):
        scenario = build_approved_pcs_scenario()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated unexpected failure deep inside a check")

        monkeypatch.setattr("src.risk.engine.check_all_legs_liquid", _boom)
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_UNKNOWN_RISK]


class TestCloseRollProposalsRegressionTS004:
    """TS-004: a CLOSE/ROLL proposal that would otherwise clear every
    risk check used to receive decision=APPROVE with approved_order=None
    -- and the Order Validator's first, unguarded
    `approved_order.quantity` access then raised a plain AttributeError,
    crashing the entire pipeline call instead of producing any clean
    result. The Risk Engine must now reject CLOSE/ROLL cleanly, with its
    own ReasonCode, before ever reaching approved-order construction."""

    def test_close_action_is_rejected_cleanly_not_approved_with_no_order(self):
        from src.llm.schemas import TradeAction

        scenario = build_approved_pcs_scenario()
        close_proposal = pcs_proposal(action=TradeAction.CLOSE, proposal_id=scenario.proposal.proposal_id)
        result = _evaluate(scenario, proposal_obj=close_proposal)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_UNSUPPORTED_ACTION]
        assert result.approved_order is None
        assert result.fidelity_ticket is None

    def test_roll_action_is_rejected_cleanly_not_approved_with_no_order(self):
        from src.llm.schemas import TradeAction

        scenario = build_approved_pcs_scenario()
        roll_proposal = pcs_proposal(action=TradeAction.ROLL, proposal_id=scenario.proposal.proposal_id)
        result = _evaluate(scenario, proposal_obj=roll_proposal)
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_UNSUPPORTED_ACTION]

    def test_an_otherwise_identical_open_proposal_still_approves(self):
        """Proves the new check is scoped to non-OPEN actions only --
        it doesn't accidentally reject the platform's normal path."""
        scenario = build_approved_pcs_scenario()
        result = _evaluate(scenario)
        assert result.decision == RiskDecision.APPROVE
        assert result.approved_order is not None
