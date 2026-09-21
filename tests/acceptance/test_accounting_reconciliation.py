"""Section 15: accounting integrity. `Portfolio.cash` means "NAV not
currently committed as capital to an open position" (see
`default_portfolio_update_stage`'s own docstring in
`src/orchestration/pipeline.py`) -- a risk-budget ledger, deliberately
distinct from `PaperBroker`'s own literal cash-on-hand balance (which
moves by the real net premium and commission; see
`test_paper_broker.py`). The identity this file proves is the one that
actually governs `Portfolio`: after any fill,

    (nav - cash) - total_capital_at_risk(portfolio)

is UNCHANGED -- exactly `capital_at_risk` worth of cash moves out of
`cash` and into the sum tracked across `positions`, never more, never
less, and never silently dropped or duplicated. This is "no
unexplained money creation/destruction" made concrete and testable.
"""
from __future__ import annotations

import pytest

from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline
from src.risk.portfolio_risk import total_capital_at_risk

from .conftest import NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_portfolio, pipeline_request


def _slack(portfolio) -> float:
    """The portion of `nav - cash` NOT yet accounted for by any open
    position's own `capital_at_risk` -- must be conserved across every
    fill, whatever its value happens to start at for a given fixture
    portfolio (it is not always zero; some fixtures start with cash
    already below nav for reasons unrelated to this pipeline)."""
    return (portfolio.nav - portfolio.cash) - total_capital_at_risk(portfolio)


class TestNavIdentityConservedAcrossASingleFill:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", [
        StrategyType.PUT_CREDIT_SPREAD, StrategyType.LONG_CALL, StrategyType.LONG_CALL_BUTTERFLY,
        StrategyType.SHORT_IRON_CONDOR, StrategyType.SHORT_IRON_BUTTERFLY,
    ])
    async def test_slack_between_nav_and_cash_is_unchanged_by_a_fill(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        market_data = make_chain(*fx.contracts)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])

        slack_before = _slack(fx.portfolio)
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.updated_portfolio is not None
        slack_after = _slack(outcome.updated_portfolio)
        assert slack_after == pytest.approx(slack_before, abs=1e-6)

    @pytest.mark.asyncio
    async def test_cash_decreases_by_exactly_the_new_positions_capital_at_risk_no_more_no_less(self):
        fx = all_strategy_fixtures()[StrategyType.SHORT_IRON_CONDOR]
        market_data = make_chain(*fx.contracts)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.SHORT_IRON_CONDOR])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        new_position = outcome.updated_portfolio.positions[-1]
        cash_delta = outcome.updated_portfolio.cash - fx.portfolio.cash
        assert cash_delta == pytest.approx(-new_position.capital_at_risk, abs=1e-6)
        # nav is never touched by a fill -- only cash and positions move;
        # nav changes only from a realized P&L event, never from opening
        # a position.
        assert outcome.updated_portfolio.nav == pytest.approx(fx.portfolio.nav)


class TestNavIdentityConservedAcrossSequentialFillsOnTheSamePortfolio:
    @pytest.mark.asyncio
    async def test_two_sequential_fills_conserve_slack_additively(self):
        """Feeds `updated_portfolio` from one pipeline run back in as
        the `portfolio` for the next -- the realistic shape of how a
        real trading day accumulates positions -- and proves the
        conservation identity holds after BOTH fills, not just one."""
        fx1 = all_strategy_fixtures()[StrategyType.LONG_CALL]
        fx2 = all_strategy_fixtures()[StrategyType.LONG_PUT]
        starting_portfolio = make_portfolio(nav=2_000_000.0, cash=1_900_000.0, peak_equity=2_000_000.0)
        slack_start = _slack(starting_portfolio)

        market_data_1 = make_chain(*fx1.contracts)
        proposal_1 = base_proposal(fx1.strategy, fx1.legs, contracts_requested=1)
        stages_1 = full_stages(market_data=market_data_1)
        req_1 = pipeline_request(proposal=proposal_1, market_data=market_data_1, portfolio=starting_portfolio, allowed_strategies=[StrategyType.LONG_CALL])
        outcome_1 = await run_order_pipeline(req_1, stages_1)
        assert outcome_1.status == PipelineStatus.FILLED

        market_data_2 = make_chain(*fx2.contracts)
        proposal_2 = base_proposal(fx2.strategy, fx2.legs, contracts_requested=1, proposal_id="prop-1")
        stages_2 = full_stages(market_data=market_data_2)
        req_2 = pipeline_request(proposal=proposal_2, market_data=market_data_2, portfolio=outcome_1.updated_portfolio, allowed_strategies=[StrategyType.LONG_PUT])
        outcome_2 = await run_order_pipeline(req_2, stages_2)
        assert outcome_2.status == PipelineStatus.FILLED

        assert len(outcome_2.updated_portfolio.positions) == 2
        slack_end = _slack(outcome_2.updated_portfolio)
        assert slack_end == pytest.approx(slack_start, abs=1e-6)


class TestPartialFillScalesCapitalAtRiskProportionally:
    @pytest.mark.asyncio
    async def test_capital_at_risk_recorded_is_scaled_by_the_actual_fill_ratio_not_the_full_request(self):
        """A partial fill must record capital_at_risk proportional to
        what ACTUALLY filled, never the full originally-requested
        size -- otherwise the portfolio would show risk for contracts
        that were never actually opened (or the opposite: opened risk
        the accounting doesn't know about)."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        # volume=10/open_interest=150 clears the Risk Engine's own
        # min_volume=10/min_open_interest=100 liquidity gate, while a
        # tight `max_fill_fraction_of_volume` still caps PaperBroker's
        # own fillable quantity well below the 10 requested.
        thin_contract = fx.contracts[0].model_copy(update={"volume": 10, "open_interest": 150})
        market_data = make_chain(thin_contract)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID, max_fill_fraction_of_volume=0.5, min_guaranteed_fill_contracts=0, thin_open_interest_threshold=1_000_000), now=NOW)
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=10)
        stages = full_stages(broker=broker, market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.PARTIALLY_FILLED
        assert 0 < outcome.order.filled_quantity < 10
        new_position = outcome.updated_portfolio.positions[-1]
        expected_ratio = outcome.order.filled_quantity / 10
        assert new_position.contracts == outcome.order.filled_quantity
        assert new_position.capital_at_risk == pytest.approx(outcome.quantitative_analysis.max_loss * expected_ratio, abs=1e-6)


class TestAccountingDiscrepancyIsAnExplicitFailureNeverSilent:
    @pytest.mark.asyncio
    async def test_a_fill_that_would_drive_cash_negative_is_flagged_not_silently_applied(self):
        """When the fill genuinely happened at the broker but recording
        its capital_at_risk against the portfolio would drive `cash`
        negative, `default_portfolio_update_stage` raises rather than
        producing an invalid `Portfolio` -- the pipeline's own SY-003
        fix (see its docstring) guarantees this is never silently
        dropped: the outcome explicitly names "portfolio_update" as the
        failed stage, and `updated_portfolio` is left `None` so no
        caller can mistake this for a clean, fully-accounted fill."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        market_data = make_chain(*fx.contracts)
        # Just enough cash to clear the Risk Engine's own buying-power
        # gate for 1 contract, but a hostile/adversarial custom
        # portfolio-update stage simulates a downstream accounting bug
        # that would drive cash negative regardless -- proving the
        # pipeline's own safety net (not the Risk Engine's) is what
        # catches this class of failure.
        from src.orchestration.pipeline import default_portfolio_update_stage

        def _broken_update_stage(portfolio, proposal, qa, order):
            # Force the same negative-cash guard by starting from a
            # portfolio whose cash is already far below what this
            # fill's capital_at_risk requires.
            drained = portfolio.model_copy(update={"cash": 1.0})
            return default_portfolio_update_stage(drained, proposal, qa, order)

        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        stages = full_stages(market_data=market_data, portfolio_update_stage=_broken_update_stage)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.rejected_stage == "portfolio_update"
        assert outcome.updated_portfolio is None
        assert "portfolio update failed" in outcome.reason
        # The real fill already happened at the broker -- the outcome
        # status still honestly reflects that a fill occurred (never
        # silently reported as REJECTED, which would hide a real
        # position from view), while `rejected_stage` is the signal
        # that accounting needs manual attention.
        assert outcome.status in (PipelineStatus.FILLED, PipelineStatus.PARTIALLY_FILLED)
        assert outcome.order is not None and outcome.order.filled_quantity > 0
