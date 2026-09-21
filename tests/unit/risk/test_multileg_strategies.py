"""Step 20A: end-to-end tests for LONG_CALL_BUTTERFLY, SHORT_IRON_CONDOR,
and SHORT_IRON_BUTTERFLY now that `TradeProposal.legs` caps at 4 and
`StrategyType` carries all 3. Covers: TradeProposal structural
validation (valid construction plus every named malformed-structure
rejection), the trusted-kernel dispatch (`check_collateral`/
`resolve_credit`/`compute_trade_economics`), and full
`evaluate_trade_proposal` runs producing a 3-/4-leg `ApprovedOrder` and
`FidelityTradeTicket` with correct per-leg quantities, both
breakevens, and a correctly signed net credit/debit.
"""
from __future__ import annotations

import math
from datetime import date

import pytest
from pydantic import ValidationError

from src.brokers.fidelity import render_ticket_text
from src.data.option_chain import OptionChain
from src.data.option_chain import OptionRight as DataOptionRight
from src.llm.schemas import Conviction, LegSide, OptionLeg, StrategyType, TradeDirection, TradeProposal
from src.llm.schemas import OptionRight as ProposalOptionRight
from src.risk.engine import evaluate_trade_proposal
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import (
    check_collateral,
    compute_trade_economics,
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
    quantitative_analysis_from,
)


def _proposal(strategy: StrategyType, legs: list[OptionLeg], **overrides) -> TradeProposal:
    base = dict(
        proposal_id="p-multileg-1", timestamp=NOW, ticker="SPY", strategy=strategy, market_regime="normal",
        expiration=EXPIRATION, legs=legs, direction=TradeDirection.NEUTRAL, contracts_requested=1,
        target_entry=1.0, profit_target=0.5, management_dte=21, thesis="t", risk_thesis="r",
        confidence=Conviction.MEDIUM, data_sources=["mock"], data_timestamp=MD_TS,
        invalidation_conditions=["thesis invalidated"],
    )
    base.update(overrides)
    return TradeProposal(**base)


def _chain(*contracts) -> OptionChain:
    return OptionChain(underlying=make_underlying(), contracts=list(contracts), timestamp=MD_TS, source="mock")


def _butterfly_legs(lower=95.0, middle=100.0, upper=105.0) -> list[OptionLeg]:
    return [
        OptionLeg(right=ProposalOptionRight.CALL, strike=lower, side=LegSide.BUY, quantity_ratio=1),
        OptionLeg(right=ProposalOptionRight.CALL, strike=middle, side=LegSide.SELL, quantity_ratio=2),
        OptionLeg(right=ProposalOptionRight.CALL, strike=upper, side=LegSide.BUY, quantity_ratio=1),
    ]


def _iron_condor_legs(long_put=590.0, short_put=610.0, short_call=650.0, long_call=670.0) -> list[OptionLeg]:
    return [
        OptionLeg(right=ProposalOptionRight.PUT, strike=long_put, side=LegSide.BUY),
        OptionLeg(right=ProposalOptionRight.PUT, strike=short_put, side=LegSide.SELL),
        OptionLeg(right=ProposalOptionRight.CALL, strike=short_call, side=LegSide.SELL),
        OptionLeg(right=ProposalOptionRight.CALL, strike=long_call, side=LegSide.BUY),
    ]


def _iron_butterfly_legs(put_wing=600.0, center=628.0, call_wing=656.0) -> list[OptionLeg]:
    return [
        OptionLeg(right=ProposalOptionRight.PUT, strike=put_wing, side=LegSide.BUY),
        OptionLeg(right=ProposalOptionRight.PUT, strike=center, side=LegSide.SELL),
        OptionLeg(right=ProposalOptionRight.CALL, strike=center, side=LegSide.SELL),
        OptionLeg(right=ProposalOptionRight.CALL, strike=call_wing, side=LegSide.BUY),
    ]


class TestLongCallButterflyStructureValidation:
    def test_valid_structure_accepted(self):
        _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs())  # does not raise

    def test_wrong_leg_count_rejected(self):
        with pytest.raises(ValidationError, match="exactly three legs"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs()[:2])

    def test_puts_instead_of_calls_rejected(self):
        legs = [
            OptionLeg(right=ProposalOptionRight.PUT, strike=95.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.SELL, quantity_ratio=2),
            OptionLeg(right=ProposalOptionRight.CALL, strike=105.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="must all be calls"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, legs)

    def test_asymmetric_wings_rejected(self):
        with pytest.raises(ValidationError, match="equally spaced"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs(lower=90.0))

    def test_wrong_side_on_wings_rejected(self):
        legs = [
            OptionLeg(right=ProposalOptionRight.CALL, strike=95.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.SELL, quantity_ratio=2),
            OptionLeg(right=ProposalOptionRight.CALL, strike=105.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="long wings and a short middle strike"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, legs)

    def test_wrong_quantity_ratio_rejected(self):
        legs = [
            OptionLeg(right=ProposalOptionRight.CALL, strike=95.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.SELL, quantity_ratio=1),  # should be 2
            OptionLeg(right=ProposalOptionRight.CALL, strike=105.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="1:-2:1 quantity ratio"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, legs)

    def test_duplicate_strikes_rejected(self):
        legs = [
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.SELL, quantity_ratio=2),
            OptionLeg(right=ProposalOptionRight.CALL, strike=105.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="strictly increasing strikes"):
            _proposal(StrategyType.LONG_CALL_BUTTERFLY, legs)

    def test_quantity_ratio_out_of_range_rejected_at_leg_level(self):
        with pytest.raises(ValidationError):
            OptionLeg(right=ProposalOptionRight.CALL, strike=100.0, side=LegSide.SELL, quantity_ratio=3)


class TestShortIronCondorStructureValidation:
    def test_valid_structure_accepted(self):
        _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs())  # does not raise

    def test_wrong_leg_count_rejected(self):
        with pytest.raises(ValidationError, match="exactly four legs"):
            _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs()[:3])

    def test_missing_wing_rejected(self):
        legs = _iron_condor_legs()
        legs[0] = OptionLeg(right=ProposalOptionRight.PUT, strike=605.0, side=LegSide.SELL)  # two short puts, no long put wing
        with pytest.raises(ValidationError):
            _proposal(StrategyType.SHORT_IRON_CONDOR, legs)

    def test_wrong_strike_order_rejected(self):
        with pytest.raises(ValidationError):
            _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs(long_put=615.0, short_put=610.0))

    def test_wrong_side_rejected(self):
        legs = _iron_condor_legs()
        legs[1] = OptionLeg(right=ProposalOptionRight.PUT, strike=610.0, side=LegSide.BUY)  # short put flipped to long
        with pytest.raises(ValidationError):
            _proposal(StrategyType.SHORT_IRON_CONDOR, legs)

    def test_nonuniform_quantity_ratio_rejected(self):
        legs = _iron_condor_legs()
        legs[1] = legs[1].model_copy(update={"quantity_ratio": 2})
        with pytest.raises(ValidationError, match="1:1:1:1 quantity ratio"):
            _proposal(StrategyType.SHORT_IRON_CONDOR, legs)


class TestShortIronButterflyStructureValidation:
    def test_valid_structure_accepted(self):
        _proposal(StrategyType.SHORT_IRON_BUTTERFLY, _iron_butterfly_legs())  # does not raise

    def test_wrong_leg_count_rejected(self):
        with pytest.raises(ValidationError, match="exactly four legs"):
            _proposal(StrategyType.SHORT_IRON_BUTTERFLY, _iron_butterfly_legs()[:2])

    def test_mismatched_center_strikes_rejected(self):
        legs = [
            OptionLeg(right=ProposalOptionRight.PUT, strike=600.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.PUT, strike=628.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.CALL, strike=630.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.CALL, strike=656.0, side=LegSide.BUY),
        ]
        with pytest.raises(ValidationError, match="same center strike"):
            _proposal(StrategyType.SHORT_IRON_BUTTERFLY, legs)

    def test_never_confused_with_long_iron_butterfly(self):
        """A long iron butterfly (both center legs BOUGHT, wings SOLD) is
        a different, unsupported structure -- must be rejected, not
        silently accepted as the short variant."""
        legs = [
            OptionLeg(right=ProposalOptionRight.PUT, strike=600.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.PUT, strike=628.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=628.0, side=LegSide.BUY),
            OptionLeg(right=ProposalOptionRight.CALL, strike=656.0, side=LegSide.SELL),
        ]
        with pytest.raises(ValidationError):
            _proposal(StrategyType.SHORT_IRON_BUTTERFLY, legs)


class TestCrossStrategyLegCountsStillWork:
    """Section 25/26: 1-leg/2-leg/3-leg/4-leg strategies all still
    validate after the cap widened from 2 to 4."""

    def test_one_leg_long_call_still_works(self):
        _proposal(StrategyType.LONG_CALL, [OptionLeg(right=ProposalOptionRight.CALL, strike=630.0, side=LegSide.BUY)])

    def test_two_leg_credit_spread_still_works(self):
        _proposal(StrategyType.PUT_CREDIT_SPREAD, [
            OptionLeg(right=ProposalOptionRight.PUT, strike=620.0, side=LegSide.SELL),
            OptionLeg(right=ProposalOptionRight.PUT, strike=615.0, side=LegSide.BUY),
        ])

    def test_three_leg_butterfly_works(self):
        _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs())

    def test_four_leg_iron_condor_works(self):
        _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs())

    def test_five_legs_rejected_at_schema_level(self):
        legs = _iron_condor_legs() + [OptionLeg(right=ProposalOptionRight.CALL, strike=700.0, side=LegSide.BUY)]
        with pytest.raises(ValidationError):
            _proposal(StrategyType.SHORT_IRON_CONDOR, legs)


class TestCheckCollateralMultiLeg:
    @pytest.mark.parametrize(
        "strategy,legs",
        [
            (StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs()),
            (StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs()),
            (StrategyType.SHORT_IRON_BUTTERFLY, _iron_butterfly_legs()),
        ],
    )
    def test_defined_risk_structures_need_no_extra_collateral(self, strategy, legs):
        check_collateral(_proposal(strategy, legs), make_portfolio(), contracts=1)  # does not raise


class TestResolveCreditAndEconomicsDispatch:
    def test_long_call_butterfly_resolves_to_positive_debit(self):
        proposal = _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs())
        contracts = [
            make_contract(option_symbol="c95", strike=95.0, right=DataOptionRight.CALL, bid=6.4, ask=6.5, underlying_price=100.0),
            make_contract(option_symbol="c100", strike=100.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=100.0),
            make_contract(option_symbol="c105", strike=105.0, right=DataOptionRight.CALL, bid=1.4, ask=1.5, underlying_price=100.0),
        ]
        debit = resolve_credit(proposal, contracts)
        assert debit > 0

    def test_short_iron_condor_resolves_to_positive_credit(self):
        proposal = _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs())
        contracts = [
            make_contract(option_symbol="p590", strike=590.0, right=DataOptionRight.PUT, bid=0.55, ask=0.6, underlying_price=628.0),
            make_contract(option_symbol="p610", strike=610.0, right=DataOptionRight.PUT, bid=1.2, ask=1.3, underlying_price=628.0),
            make_contract(option_symbol="c650", strike=650.0, right=DataOptionRight.CALL, bid=1.15, ask=1.2, underlying_price=628.0),
            make_contract(option_symbol="c670", strike=670.0, right=DataOptionRight.CALL, bid=0.47, ask=0.5, underlying_price=628.0),
        ]
        credit = resolve_credit(proposal, contracts)
        assert credit > 0

    def test_long_call_butterfly_economics_finite_and_defined_risk(self):
        proposal = _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs())
        contracts = [
            make_contract(option_symbol="c95", strike=95.0, right=DataOptionRight.CALL, bid=6.4, ask=6.5, underlying_price=100.0),
            make_contract(option_symbol="c100", strike=100.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=100.0),
            make_contract(option_symbol="c105", strike=105.0, right=DataOptionRight.CALL, bid=1.4, ask=1.5, underlying_price=100.0),
        ]
        market_data = _chain(*contracts)
        limits = default_limits()
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, make_portfolio(), limits, num_contracts=1)
        assert math.isfinite(econ.max_profit)
        assert math.isfinite(econ.max_loss)
        assert econ.max_loss >= 0
        assert econ.breakeven_upper is not None  # two breakevens: lower and upper

    def test_short_iron_condor_economics_defined_risk_with_two_breakevens(self):
        proposal = _proposal(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs())
        contracts = [
            make_contract(option_symbol="p590", strike=590.0, right=DataOptionRight.PUT, bid=0.55, ask=0.6, underlying_price=628.0),
            make_contract(option_symbol="p610", strike=610.0, right=DataOptionRight.PUT, bid=1.2, ask=1.3, underlying_price=628.0),
            make_contract(option_symbol="c650", strike=650.0, right=DataOptionRight.CALL, bid=1.15, ask=1.2, underlying_price=628.0),
            make_contract(option_symbol="c670", strike=670.0, right=DataOptionRight.CALL, bid=0.47, ask=0.5, underlying_price=628.0),
        ]
        market_data = _chain(*contracts)
        limits = default_limits()
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, make_portfolio(), limits, num_contracts=1)
        assert math.isfinite(econ.max_profit)
        assert math.isfinite(econ.max_loss)
        assert econ.breakeven_upper is not None
        assert econ.breakeven_upper > econ.breakeven

    def test_short_iron_butterfly_economics_defined_risk(self):
        proposal = _proposal(StrategyType.SHORT_IRON_BUTTERFLY, _iron_butterfly_legs())
        contracts = [
            make_contract(option_symbol="p600", strike=600.0, right=DataOptionRight.PUT, bid=0.85, ask=0.9, underlying_price=628.0),
            make_contract(option_symbol="p628", strike=628.0, right=DataOptionRight.PUT, bid=3.2, ask=3.3, underlying_price=628.0),
            make_contract(option_symbol="c628", strike=628.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=628.0),
            make_contract(option_symbol="c656", strike=656.0, right=DataOptionRight.CALL, bid=0.95, ask=1.0, underlying_price=628.0),
        ]
        market_data = _chain(*contracts)
        limits = default_limits()
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, make_portfolio(), limits, num_contracts=1)
        assert math.isfinite(econ.max_profit)
        assert math.isfinite(econ.max_loss)
        assert econ.breakeven_upper is not None


class TestEndToEndRiskEngineIntegration:
    """Full evaluate_trade_proposal runs: proves the Risk Engine
    approves the 3 new strategies, produces a correctly-shaped
    ApprovedOrder/FidelityTradeTicket (right leg count, right
    quantities), and never treats the structures as naked."""

    def _run(self, strategy: StrategyType, legs: list[OptionLeg], contracts) -> tuple:
        proposal = _proposal(strategy, legs, contracts_requested=1)
        market_data = _chain(*contracts)
        limits = default_limits()
        portfolio = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, sector_by_ticker={"SPY": "INDEX"})
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        qa = quantitative_analysis_from(proposal, resolved, portfolio, limits)
        capabilities = fidelity_capabilities(allowed_strategies=[
            "CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD",
            "LONG_CALL_BUTTERFLY", "SHORT_IRON_CONDOR", "SHORT_IRON_BUTTERFLY",
        ])
        result = evaluate_trade_proposal(proposal, portfolio, qa, market_data, capabilities, now=NOW)
        return proposal, result

    def test_long_call_butterfly_approved_with_three_legs_and_correct_ratio(self):
        contracts = [
            make_contract(option_symbol="c95", strike=95.0, right=DataOptionRight.CALL, bid=6.4, ask=6.5, underlying_price=100.0),
            make_contract(option_symbol="c100", strike=100.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=100.0),
            make_contract(option_symbol="c105", strike=105.0, right=DataOptionRight.CALL, bid=1.4, ask=1.5, underlying_price=100.0),
        ]
        _, result = self._run(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs(), contracts)
        assert result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)
        assert result.fidelity_ticket is not None
        assert len(result.fidelity_ticket.legs) == 3
        # middle leg carries 2x the contracts of each wing -- the 1:-2:1 ratio
        by_strike = {leg.strike: leg.contracts for leg in result.fidelity_ticket.legs}
        assert by_strike[100.0] == 2 * by_strike[95.0]
        assert by_strike[100.0] == 2 * by_strike[105.0]
        assert result.fidelity_ticket.estimated_credit_debit < 0  # net debit
        rendered = render_ticket_text(result.fidelity_ticket)
        assert "LEG 1:" in rendered and "LEG 2:" in rendered and "LEG 3:" in rendered
        assert "NET DEBIT" in rendered

    def test_short_iron_condor_approved_with_four_legs_defined_risk(self):
        contracts = [
            make_contract(option_symbol="p590", strike=590.0, right=DataOptionRight.PUT, bid=0.55, ask=0.6, underlying_price=628.0),
            make_contract(option_symbol="p610", strike=610.0, right=DataOptionRight.PUT, bid=1.2, ask=1.3, underlying_price=628.0),
            make_contract(option_symbol="c650", strike=650.0, right=DataOptionRight.CALL, bid=1.15, ask=1.2, underlying_price=628.0),
            make_contract(option_symbol="c670", strike=670.0, right=DataOptionRight.CALL, bid=0.47, ask=0.5, underlying_price=628.0),
        ]
        proposal, result = self._run(StrategyType.SHORT_IRON_CONDOR, _iron_condor_legs(), contracts)
        assert result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)
        assert result.fidelity_ticket is not None
        assert len(result.fidelity_ticket.legs) == 4
        assert result.fidelity_ticket.estimated_credit_debit > 0  # net credit
        assert result.fidelity_ticket.breakeven_upper is not None
        # defined risk: never the naive naked-short capital figure
        assert result.max_loss < 620.0 * 100  # far below a naked short put's own strike-based exposure
        rendered = render_ticket_text(result.fidelity_ticket)
        assert "LEG 4:" in rendered
        assert "BREAKEVEN (LOWER):" in rendered and "BREAKEVEN (UPPER):" in rendered

    def test_short_iron_butterfly_approved_with_four_legs_defined_risk(self):
        contracts = [
            make_contract(option_symbol="p600", strike=600.0, right=DataOptionRight.PUT, bid=0.85, ask=0.9, underlying_price=628.0),
            make_contract(option_symbol="p628", strike=628.0, right=DataOptionRight.PUT, bid=3.2, ask=3.3, underlying_price=628.0),
            make_contract(option_symbol="c628", strike=628.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=628.0),
            make_contract(option_symbol="c656", strike=656.0, right=DataOptionRight.CALL, bid=0.95, ask=1.0, underlying_price=628.0),
        ]
        _, result = self._run(StrategyType.SHORT_IRON_BUTTERFLY, _iron_butterfly_legs(), contracts)
        assert result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)
        assert result.fidelity_ticket is not None
        assert len(result.fidelity_ticket.legs) == 4
        assert result.fidelity_ticket.estimated_credit_debit > 0
        assert math.isfinite(result.max_loss)

    def test_missing_broker_capability_rejects(self):
        """Never infer Fidelity options approval -- an allowed_strategies
        list that omits the new strategy must reject it."""
        contracts = [
            make_contract(option_symbol="c95", strike=95.0, right=DataOptionRight.CALL, bid=6.4, ask=6.5, underlying_price=100.0),
            make_contract(option_symbol="c100", strike=100.0, right=DataOptionRight.CALL, bid=3.4, ask=3.5, underlying_price=100.0),
            make_contract(option_symbol="c105", strike=105.0, right=DataOptionRight.CALL, bid=1.4, ask=1.5, underlying_price=100.0),
        ]
        proposal = _proposal(StrategyType.LONG_CALL_BUTTERFLY, _butterfly_legs(), contracts_requested=1)
        market_data = _chain(*contracts)
        limits = default_limits()
        portfolio = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, sector_by_ticker={"SPY": "INDEX"})
        resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        qa = quantitative_analysis_from(proposal, resolved, portfolio, limits)
        capabilities = fidelity_capabilities()  # deliberately omits the new strategies
        result = evaluate_trade_proposal(proposal, portfolio, qa, market_data, capabilities, now=NOW)
        assert result.decision in (RiskDecision.REJECT, RiskDecision.HALT)
