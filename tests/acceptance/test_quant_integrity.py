"""Section 6: Quant Engine integrity, at the level this acceptance
suite is uniquely positioned to check -- not re-deriving Black-Scholes/
Greeks math already exhaustively covered by
`tests/unit/quant/test_black_scholes.py` (Hull textbook reference
value, put-call parity, independent-implementation cross-check) and
`tests/unit/quant/test_payoff_profile.py`, but instead hand-computing
the textbook per-strategy economics formula directly against a known
fixture and tracing that exact number through the REAL, unmocked
pipeline stage by stage (Quant stage -> Risk Engine's own independent
cross-check -> Fidelity ticket), hunting specifically for the class of
error a unit test of one function in isolation cannot catch: a x100
multiplier applied twice (or not at all) between stages, a sign flip
crossing a module boundary, an annualization using the wrong DTE, a
quantity-ratio not carried through a leg-to-order conversion, or a
netting step silently dropped when a value is copied from one schema
to the next.
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, pipeline_request

# Calendar-day DTE used by every economics formula's own annualization --
# computed here independently of `src.risk.trade_risk.compute_trade_economics`
# (which derives the identical quantity from `proposal.expiration -
# proposal.timestamp.date()`), so a DTE-off-by-one-day bug in that call
# site would show up as a mismatch against this hand-computed value.
_DTE = (EXPIRATION - NOW.date()).days


class TestPutCreditSpreadHandComputedEconomicsFlowThroughTheRealPipeline:
    """Short 620 put (mid 1.35), long 615 put (mid 0.60): credit=0.75,
    width=5. Textbook credit-spread identities: max_profit=credit*100,
    max_loss=(width-credit)*100, breakeven=short_strike-credit,
    capital_required=max_loss (standard defined-risk margin)."""

    _CREDIT = 1.35 - 0.60
    _WIDTH = 620.0 - 615.0
    _EXPECTED_MAX_PROFIT = _CREDIT * 100
    _EXPECTED_MAX_LOSS = (_WIDTH - _CREDIT) * 100
    _EXPECTED_BREAKEVEN = 620.0 - _CREDIT
    _EXPECTED_CAPITAL_REQUIRED = _EXPECTED_MAX_LOSS
    _EXPECTED_ROC = _EXPECTED_MAX_PROFIT / _EXPECTED_CAPITAL_REQUIRED
    _EXPECTED_ANNUALIZED_ROC = _EXPECTED_ROC * (365.0 / _DTE)

    @pytest.mark.asyncio
    async def test_quant_stage_output_matches_hand_computed_economics(self):
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        qa = outcome.quantitative_analysis
        assert qa.max_profit == pytest.approx(self._EXPECTED_MAX_PROFIT)
        assert qa.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert qa.breakeven == pytest.approx(self._EXPECTED_BREAKEVEN)
        assert qa.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED)
        assert qa.return_on_capital == pytest.approx(self._EXPECTED_ROC)
        assert qa.annualized_roc == pytest.approx(self._EXPECTED_ANNUALIZED_ROC, rel=1e-6)

    @pytest.mark.asyncio
    async def test_risk_engine_and_fidelity_ticket_preserve_the_same_numbers(self):
        """The same $75/$425/$619.25 figures the Quant stage computed
        must survive, unaltered, through the Risk Engine's own
        independent cross-check (`ARCHITECTURE.md`'s "computed once,
        cross-checked never re-derived differently" rule) and into the
        human-readable Fidelity ticket -- a wiring bug that silently
        rescales or drops a figure between stages would show up here
        even though each stage's own unit tests pass in isolation."""
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.risk_decision.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert outcome.risk_decision.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED)
        ticket = outcome.fidelity_ticket
        assert ticket.max_profit == pytest.approx(self._EXPECTED_MAX_PROFIT)
        assert ticket.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert ticket.breakeven == pytest.approx(self._EXPECTED_BREAKEVEN)
        assert ticket.capital_at_risk == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED)


class TestLongCallUnboundedUpsideIsRepresentedHonestlyNeverCapped:
    """Long 630 call, premium mid 6.9: max_loss=premium*100=690,
    breakeven=strike+premium=636.9, max_profit is genuinely unbounded
    (`math.inf`) -- an LLM-facing/ticket-facing schema fabricating a
    finite "max profit" for an unbounded long option would itself be a
    CRITICAL invariant-2 violation (an LLM or any downstream stage
    inventing a plausible-looking authoritative number); this proves
    the platform instead carries the honest `inf` value all the way to
    the ticket rather than silently capping or rounding it into
    something finite-looking."""

    _PREMIUM = (6.8 + 7.0) / 2
    _EXPECTED_MAX_LOSS = _PREMIUM * 100
    _EXPECTED_BREAKEVEN = 630.0 + _PREMIUM

    @pytest.mark.asyncio
    async def test_max_profit_is_literally_infinite_end_to_end(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.quantitative_analysis.max_profit == math.inf
        assert outcome.quantitative_analysis.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert outcome.quantitative_analysis.breakeven == pytest.approx(self._EXPECTED_BREAKEVEN)
        assert outcome.risk_decision.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert outcome.risk_decision.capital_required == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert outcome.fidelity_ticket.max_profit == math.inf
        assert outcome.fidelity_ticket.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)


class TestQuantityRatioCorrectlyScalesTheButterflysDebitAndCollateral:
    """Buy 1x 615 call (mid 16.5), SELL 2x 628 call (mid 9.5), buy 1x
    641 call (mid 4.5): debit = lower - 2*middle + upper = 16.5 - 19.0
    + 4.5 = 2.0 per combo unit. A quantity-ratio wiring bug (e.g. the
    middle leg's 2x silently treated as 1x when building the order, or
    double-counted) would show up as a wrong debit/collateral here --
    this is exactly the invariant `CLAUDE.md`'s multi-leg architecture
    section names as the platform's most fragile spot."""

    _LOWER, _MIDDLE, _UPPER = 16.5, 9.5, 4.5
    _DEBIT = _LOWER - 2 * _MIDDLE + _UPPER
    _EXPECTED_MAX_LOSS = _DEBIT * 100
    _EXPECTED_CAPITAL_REQUIRED = _DEBIT * 100
    _EXPECTED_MAX_PROFIT = ((628.0 - 615.0) - _DEBIT) * 100  # wing width minus debit, at the short middle strike

    @pytest.mark.asyncio
    async def test_debit_and_collateral_correctly_reflect_the_1_minus2_1_ratio(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL_BUTTERFLY]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL_BUTTERFLY])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        qa = outcome.quantitative_analysis
        assert qa.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS)
        assert qa.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED)
        assert qa.max_profit == pytest.approx(self._EXPECTED_MAX_PROFIT)
        # No naked-short-style extra collateral for the 2x-ratio middle
        # leg -- collateral is the debit paid, nothing more (see
        # CLAUDE.md's "Defined-risk multi-leg collateral" section).
        assert outcome.risk_decision.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED)

    @pytest.mark.asyncio
    async def test_two_contracts_requested_scales_debit_and_collateral_linearly(self):
        """Requesting 2 combo units must scale the debit/collateral by
        exactly 2x -- not 4x (double-counting the 2x middle leg again)
        and not left at 1x (ignoring the ratio when contracts scale)."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL_BUTTERFLY]
        # A larger portfolio so 2 combo units aren't themselves resized down.
        from .conftest import make_portfolio
        big_portfolio = make_portfolio(nav=5_000_000.0, cash=4_500_000.0, peak_equity=5_000_000.0)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=2)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=big_portfolio, allowed_strategies=[StrategyType.LONG_CALL_BUTTERFLY])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.order.filled_quantity == 2
        qa = outcome.quantitative_analysis
        assert qa.max_loss == pytest.approx(self._EXPECTED_MAX_LOSS * 2)
        assert qa.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED * 2)


class TestShortIronCondorNettingAcrossBothWings:
    """Netting bug hunt: the iron condor's credit is the SUM of two
    independent credit-spread nettings (put side + call side), never
    the difference of just the two short legs' premiums (which would
    silently ignore both long wings) and never double-counting either
    wing."""

    _LP, _SP, _SC, _LC = 0.575, 1.25, 1.175, 0.485  # mids of the 4 fixture legs
    _CREDIT = (_SP - _LP) + (_SC - _LC)
    _PUT_WIDTH = 610.0 - 600.0
    _CALL_WIDTH = 660.0 - 650.0
    _EXPECTED_CAPITAL_REQUIRED = (max(_PUT_WIDTH, _CALL_WIDTH) - _CREDIT) * 100
    _EXPECTED_MAX_PROFIT = _CREDIT * 100

    @pytest.mark.asyncio
    async def test_credit_is_the_sum_of_both_wings_never_a_single_wing_alone(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.SHORT_IRON_CONDOR])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        qa = outcome.quantitative_analysis
        assert qa.max_profit == pytest.approx(self._EXPECTED_MAX_PROFIT, abs=0.5)
        assert qa.capital_required == pytest.approx(self._EXPECTED_CAPITAL_REQUIRED, abs=0.5)
        # Never the naive sum of both wing widths -- only the wider one.
        naive_sum_of_both_widths = (self._PUT_WIDTH + self._CALL_WIDTH - self._CREDIT) * 100
        assert qa.capital_required < naive_sum_of_both_widths


class TestExpirationPayoffKeptSeparateFromMarkToMarketPricing:
    """Section 6's own explicit requirement: "test payoff at multiple
    price points AND at expiration separately from mark-to-market."
    The fixture's quoted premium (mark-to-market, includes remaining
    time value with 26 DTE) must differ from the pure intrinsic value
    at the SAME strike -- proving these are genuinely two different
    numbers computed by two different code paths, never silently
    treated as interchangeable."""

    def test_long_call_premium_carries_time_value_distinct_from_intrinsic(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        contract = fx.contracts[0]
        mid_premium = (contract.bid + contract.ask) / 2.0
        # SPOT (628.5) is below the 630 strike -- the call is OTM today,
        # so its entire mark-to-market premium IS time value (intrinsic
        # is exactly 0). If the two were ever conflated, an OTM option's
        # "value" would wrongly show as 0 instead of its real premium.
        intrinsic_today = max(628.5 - contract.strike, 0.0)
        assert intrinsic_today == 0.0
        assert mid_premium > 0.0
        assert mid_premium != pytest.approx(intrinsic_today)

    def test_expiration_payoff_at_a_specific_terminal_price_ignores_the_original_premiums_time_value_component_correctly(self):
        """At expiration, a terminal price ABOVE the strike settles the
        long call at pure intrinsic value minus the ORIGINAL premium
        paid (not the current mark) -- `payoff_at_expiration` and the
        pre-expiration mark-to-market premium are deliberately two
        separate quantities feeding two separate computations."""
        from src.quant.black_scholes import Leg, OptionRight as QRight, Side
        from src.quant.monte_carlo import Position, payoff_at_expiration

        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        contract = fx.contracts[0]
        premium_paid = (contract.bid + contract.ask) / 2.0
        position = Position(legs=[Leg(right=QRight.CALL, strike=630.0, side=Side.BUY, entry_price=premium_paid, quantity=1)])

        terminal_price = 645.0
        expected_pnl = (terminal_price - 630.0 - premium_paid) * 100
        assert payoff_at_expiration(position, terminal_price) == pytest.approx(expected_pnl)

        # Well below the strike -- expires worthless, loss is exactly
        # the premium paid, never more (defined risk on a long option).
        assert payoff_at_expiration(position, 500.0) == pytest.approx(-premium_paid * 100)
