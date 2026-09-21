"""Step 19A: tests for the trusted-kernel extension (check_collateral /
resolve_credit / compute_trade_economics dispatch in src.risk.trade_risk,
and the ApprovedOrder debit/credit sign fix in src.risk.engine). The
sign-correctness regression test (TestDebitOrderSignCorrectness) is the
most important test in this file — it directly proves the bug caught
during manual smoke-testing (a debit order's ticket incorrectly labeled
"NET CREDIT") stays fixed.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from src.brokers.fidelity import render_ticket_text
from src.data.option_chain import OptionChain
from src.data.option_chain import OptionRight as DataOptionRight
from src.llm.schemas import Conviction, LegSide, OptionLeg, OptionRight as ProposalOptionRight, StrategyType, TradeDirection, TradeProposal
from src.risk.engine import evaluate_trade_proposal
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import (
    MissingCollateralError,
    QuantitativeAnalysis,
    check_collateral,
    compute_trade_economics,
    cross_check_quantitative_analysis,
    resolve_credit,
    resolve_leg_contracts,
)

from .conftest import (
    EXPIRATION,
    MD_TS,
    NOW,
    default_limits,
    fidelity_capabilities,
    make_contract,
    make_portfolio,
    make_underlying,
)


def _proposal(strategy: StrategyType, legs: list[OptionLeg], **overrides) -> TradeProposal:
    base = dict(
        proposal_id="p-expanded-1", timestamp=NOW, ticker="SPY", strategy=strategy, market_regime="normal",
        expiration=EXPIRATION, legs=legs, direction=TradeDirection.NEUTRAL, contracts_requested=1,
        target_entry=1.0, profit_target=0.5, management_dte=21, thesis="t", risk_thesis="r",
        confidence=Conviction.MEDIUM, data_sources=["mock"], data_timestamp=MD_TS,
        invalidation_conditions=["thesis invalidated"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def _chain(*contracts) -> OptionChain:
    return OptionChain(underlying=make_underlying(), contracts=list(contracts), timestamp=MD_TS, source="mock")


class TestCheckCollateralNewStrategies:
    def test_bull_call_spread_needs_no_shares_or_cash_beyond_buying_power(self):
        proposal = _proposal(StrategyType.BULL_CALL_SPREAD, [
            OptionLeg(right=ProposalOptionRight.CALL, strike=620, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL),
        ])
        check_collateral(proposal, make_portfolio(), contracts=1)  # does not raise

    def test_protective_put_requires_shares(self):
        proposal = _proposal(StrategyType.PROTECTIVE_PUT, [OptionLeg(right=ProposalOptionRight.PUT, strike=600, side=LegSide.BUY)])
        with pytest.raises(MissingCollateralError, match="requires 100 shares"):
            check_collateral(proposal, make_portfolio(), contracts=1)

    def test_protective_put_passes_with_enough_shares(self):
        proposal = _proposal(StrategyType.PROTECTIVE_PUT, [OptionLeg(right=ProposalOptionRight.PUT, strike=600, side=LegSide.BUY)])
        from src.risk.portfolio_risk import UnderlyingHolding

        portfolio = make_portfolio(underlying_holdings={"SPY": UnderlyingHolding(shares=100, cost_basis=628.5)})
        check_collateral(proposal, portfolio, contracts=1)  # does not raise

    def test_protective_collar_requires_shares(self):
        proposal = _proposal(StrategyType.PROTECTIVE_COLLAR, [
            OptionLeg(right=ProposalOptionRight.CALL, strike=640, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.PUT, strike=610, side=LegSide.BUY),
        ])
        with pytest.raises(MissingCollateralError, match="requires 100 shares"):
            check_collateral(proposal, make_portfolio(), contracts=1)

    @pytest.mark.parametrize(
        "strategy,legs",
        [
            (StrategyType.LONG_CALL, [OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.BUY)]),
            (StrategyType.LONG_PUT, [OptionLeg(right=ProposalOptionRight.PUT, strike=610, side=LegSide.BUY)]),
            (StrategyType.LONG_STRADDLE, [
                OptionLeg(right=ProposalOptionRight.CALL, strike=620, side=LegSide.BUY),
                OptionLeg(right=ProposalOptionRight.PUT, strike=620, side=LegSide.BUY),
            ]),
        ],
    )
    def test_pure_long_strategies_need_no_collateral(self, strategy, legs):
        check_collateral(_proposal(strategy, legs), make_portfolio(), contracts=1)  # does not raise


class TestResolveCreditSign:
    def test_call_credit_spread_is_positive(self):
        proposal = _proposal(StrategyType.CALL_CREDIT_SPREAD, [
            OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.CALL, strike=640, side=LegSide.BUY),
        ])
        contracts = [
            make_contract(option_symbol="c1", strike=630, right=DataOptionRight.CALL, bid=2.8, ask=3.0),
            make_contract(option_symbol="c2", strike=640, right=DataOptionRight.CALL, bid=1.0, ask=1.2),
        ]
        credit = resolve_credit(proposal, contracts)
        assert credit > 0

    def test_bull_call_spread_debit_magnitude_is_positive(self):
        """resolve_credit itself always returns a positive MAGNITUDE
        (the convention src.quant.expected_value's debit-strategy
        functions expect) -- the sign that actually reaches a human is
        applied downstream in src.risk.engine._build_approved_order,
        covered by TestDebitOrderSignCorrectness below."""
        proposal = _proposal(StrategyType.BULL_CALL_SPREAD, [
            OptionLeg(right=ProposalOptionRight.CALL, strike=620, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL),
        ])
        contracts = [
            make_contract(option_symbol="c1", strike=620, right=DataOptionRight.CALL, bid=6.8, ask=7.0),
            make_contract(option_symbol="c2", strike=630, right=DataOptionRight.CALL, bid=2.3, ask=2.5),
        ]
        debit = resolve_credit(proposal, contracts)
        assert debit > 0


class TestComputeTradeEconomicsDispatch:
    @pytest.mark.parametrize(
        "strategy,legs,contracts",
        [
            (
                StrategyType.BULL_CALL_SPREAD,
                [OptionLeg(right=ProposalOptionRight.CALL, strike=620, side=LegSide.BUY), OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL)],
                [make_contract(option_symbol="c1", strike=620, right=DataOptionRight.CALL, bid=6.8, ask=7.0), make_contract(option_symbol="c2", strike=630, right=DataOptionRight.CALL, bid=2.3, ask=2.5)],
            ),
            (
                StrategyType.BEAR_PUT_SPREAD,
                [OptionLeg(right=ProposalOptionRight.PUT, strike=630, side=LegSide.BUY), OptionLeg(right=ProposalOptionRight.PUT, strike=620, side=LegSide.SELL)],
                [make_contract(option_symbol="p1", strike=630, right=DataOptionRight.PUT, bid=6.0, ask=6.2), make_contract(option_symbol="p2", strike=620, right=DataOptionRight.PUT, bid=2.0, ask=2.2)],
            ),
            (
                StrategyType.CALL_CREDIT_SPREAD,
                [OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL), OptionLeg(right=ProposalOptionRight.CALL, strike=640, side=LegSide.BUY)],
                [make_contract(option_symbol="c1", strike=630, right=DataOptionRight.CALL, bid=2.8, ask=3.0), make_contract(option_symbol="c2", strike=640, right=DataOptionRight.CALL, bid=1.0, ask=1.2)],
            ),
            (
                StrategyType.LONG_CALL,
                [OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.BUY)],
                [make_contract(option_symbol="c1", strike=630, right=DataOptionRight.CALL, bid=2.8, ask=3.0)],
            ),
            (
                StrategyType.LONG_PUT,
                [OptionLeg(right=ProposalOptionRight.PUT, strike=610, side=LegSide.BUY)],
                [make_contract(option_symbol="p1", strike=610, right=DataOptionRight.PUT, bid=2.5, ask=2.7)],
            ),
            (
                StrategyType.LONG_STRADDLE,
                [OptionLeg(right=ProposalOptionRight.CALL, strike=628, side=LegSide.BUY), OptionLeg(right=ProposalOptionRight.PUT, strike=628, side=LegSide.BUY)],
                [make_contract(option_symbol="c1", strike=628, right=DataOptionRight.CALL, bid=3.0, ask=3.2), make_contract(option_symbol="p1", strike=628, right=DataOptionRight.PUT, bid=3.1, ask=3.3)],
            ),
            (
                StrategyType.LONG_STRANGLE,
                [OptionLeg(right=ProposalOptionRight.CALL, strike=635, side=LegSide.BUY), OptionLeg(right=ProposalOptionRight.PUT, strike=620, side=LegSide.BUY)],
                [make_contract(option_symbol="c1", strike=635, right=DataOptionRight.CALL, bid=1.4, ask=1.6), make_contract(option_symbol="p1", strike=620, right=DataOptionRight.PUT, bid=1.3, ask=1.5)],
            ),
        ],
    )
    def test_dispatch_produces_finite_defined_risk_economics(self, strategy, legs, contracts):
        proposal = _proposal(strategy, legs, contracts_requested=1)
        market_data = _chain(*contracts)
        limits = default_limits()
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, make_portfolio(), limits, num_contracts=1)
        assert math.isfinite(econ.max_loss)
        assert econ.max_loss >= 0

    def test_protective_put_dispatch_needs_holding(self):
        from src.risk.portfolio_risk import UnderlyingHolding

        proposal = _proposal(StrategyType.PROTECTIVE_PUT, [OptionLeg(right=ProposalOptionRight.PUT, strike=610, side=LegSide.BUY)])
        contract = make_contract(option_symbol="p1", strike=610, right=DataOptionRight.PUT, bid=2.5, ask=2.7)
        market_data = _chain(contract)
        limits = default_limits()
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        portfolio = make_portfolio(underlying_holdings={"SPY": UnderlyingHolding(shares=100, cost_basis=628.5)})
        econ = compute_trade_economics(proposal, resolved, portfolio, limits, num_contracts=1)
        assert econ.max_profit == math.inf


class TestQuantitativeAnalysisInfinityHandling:
    def test_max_profit_accepts_positive_infinity(self):
        qa = QuantitativeAnalysis(
            proposal_id="p1", generated_at=MD_TS, max_profit=math.inf, max_loss=300.0, breakeven=103.0,
            capital_required=300.0, return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4,
            expected_value=10.0, net_delta=50.0, net_vega=5.0,
        )
        assert qa.max_profit == math.inf

    def test_max_profit_rejects_negative_infinity(self):
        with pytest.raises(Exception):
            QuantitativeAnalysis(
                proposal_id="p1", generated_at=MD_TS, max_profit=-math.inf, max_loss=300.0, breakeven=103.0,
                capital_required=300.0, return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4,
                expected_value=10.0, net_delta=50.0, net_vega=5.0,
            )

    def test_max_profit_rejects_nan(self):
        with pytest.raises(Exception):
            QuantitativeAnalysis(
                proposal_id="p1", generated_at=MD_TS, max_profit=math.nan, max_loss=300.0, breakeven=103.0,
                capital_required=300.0, return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4,
                expected_value=10.0, net_delta=50.0, net_vega=5.0,
            )

    def test_max_loss_still_rejects_infinity(self):
        with pytest.raises(Exception):
            QuantitativeAnalysis(
                proposal_id="p1", generated_at=MD_TS, max_profit=100.0, max_loss=math.inf, breakeven=103.0,
                capital_required=300.0, return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4,
                expected_value=10.0, net_delta=50.0, net_vega=5.0,
            )

    def test_cross_check_treats_matching_infinities_as_agreement(self):
        from src.quant.expected_value import StrategyEconomics

        supplied = QuantitativeAnalysis(
            proposal_id="p1", generated_at=MD_TS, max_profit=math.inf, max_loss=300.0, breakeven=103.0,
            capital_required=300.0, return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4,
            expected_value=10.0, net_delta=50.0, net_vega=5.0,
        )
        recomputed = StrategyEconomics(
            max_profit=math.inf, max_loss=300.0, breakeven=103.0, capital_required=300.0,
            return_on_capital=0.5, annualized_roc=1.0, probability_of_profit=0.4, expected_value=10.0,
        )
        cross_check_quantitative_analysis(supplied, recomputed, default_limits(), num_contracts=1)  # does not raise


class TestDebitOrderSignCorrectness:
    """The core regression test for the bug caught during manual
    end-to-end smoke testing: a bull call spread (a net-DEBIT structure)
    must produce a Fidelity ticket labeled "NET DEBIT", never "NET
    CREDIT" -- getting this wrong would actively mislead a human
    entering the order manually in Trader+."""

    def test_bull_call_spread_ticket_says_net_debit(self):
        proposal = _proposal(
            StrategyType.BULL_CALL_SPREAD,
            [
                OptionLeg(right=ProposalOptionRight.CALL, strike=620, side=LegSide.BUY),
                OptionLeg(right=ProposalOptionRight.CALL, strike=630, side=LegSide.SELL),
            ],
            contracts_requested=1, direction=TradeDirection.BULLISH,
        )
        contracts = [
            make_contract(option_symbol="c1", strike=620, right=DataOptionRight.CALL, bid=6.8, ask=7.0),
            make_contract(option_symbol="c2", strike=630, right=DataOptionRight.CALL, bid=2.3, ask=2.5),
        ]
        market_data = _chain(*contracts)
        limits = default_limits()
        portfolio = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0)  # large enough NAV to size >=1 contract cleanly
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, portfolio, limits, num_contracts=1)
        qa = QuantitativeAnalysis(
            proposal_id=proposal.proposal_id, generated_at=MD_TS, max_profit=econ.max_profit, max_loss=econ.max_loss,
            breakeven=econ.breakeven, breakeven_upper=econ.breakeven_upper, capital_required=econ.capital_required,
            return_on_capital=econ.return_on_capital, annualized_roc=econ.annualized_roc,
            probability_of_profit=econ.probability_of_profit, expected_value=econ.expected_value,
            net_delta=0.0, net_vega=0.0,
        )
        result = evaluate_trade_proposal(proposal, portfolio, qa, market_data, fidelity_capabilities(
            allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD", "BULL_CALL_SPREAD"]
        ), now=NOW)
        assert result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)
        assert result.fidelity_ticket is not None
        assert result.fidelity_ticket.estimated_credit_debit < 0
        assert result.fidelity_ticket.limit_price > 0  # always a positive magnitude, per ApprovedOrder's own Field(gt=0)
        assert result.fidelity_ticket.minimum_acceptable_price >= result.fidelity_ticket.limit_price
        rendered = render_ticket_text(result.fidelity_ticket)
        assert "NET DEBIT" in rendered
        assert "NET CREDIT" not in rendered

    def test_put_credit_spread_ticket_still_says_net_credit(self):
        """The original 3 strategies' behavior is unchanged by this
        refactor -- a credit spread still renders NET CREDIT."""
        from .conftest import build_approved_pcs_scenario

        scenario = build_approved_pcs_scenario()
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis, scenario.market_data,
            scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.fidelity_ticket is not None
        assert result.fidelity_ticket.estimated_credit_debit > 0
        rendered = render_ticket_text(result.fidelity_ticket)
        assert "NET CREDIT" in rendered
        assert "NET DEBIT" not in rendered

    def test_max_profit_unlimited_renders_as_unlimited_not_dollar_inf(self):
        from src.risk.portfolio_risk import UnderlyingHolding

        proposal = _proposal(
            StrategyType.PROTECTIVE_PUT, [OptionLeg(right=ProposalOptionRight.PUT, strike=610, side=LegSide.BUY)],
            direction=TradeDirection.BEARISH,
        )
        contract = make_contract(option_symbol="p1", strike=610, right=DataOptionRight.PUT, bid=2.5, ask=2.7)
        market_data = _chain(contract)
        limits = default_limits()
        portfolio = make_portfolio(
            nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, underlying_holdings={"SPY": UnderlyingHolding(shares=100, cost_basis=628.5)},
        )
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, portfolio, limits, num_contracts=1)
        qa = QuantitativeAnalysis(
            proposal_id=proposal.proposal_id, generated_at=MD_TS, max_profit=econ.max_profit, max_loss=econ.max_loss,
            breakeven=econ.breakeven, capital_required=econ.capital_required, return_on_capital=econ.return_on_capital,
            annualized_roc=econ.annualized_roc, probability_of_profit=econ.probability_of_profit,
            expected_value=econ.expected_value, net_delta=0.0, net_vega=0.0,
        )
        result = evaluate_trade_proposal(proposal, portfolio, qa, market_data, fidelity_capabilities(
            allowed_strategies=["CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD", "PROTECTIVE_PUT"]
        ), now=NOW)
        assert result.fidelity_ticket is not None
        assert result.fidelity_ticket.max_profit == math.inf
        rendered = render_ticket_text(result.fidelity_ticket)
        assert "UNLIMITED" in rendered
        assert "$inf" not in rendered
