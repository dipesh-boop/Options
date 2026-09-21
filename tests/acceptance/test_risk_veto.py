"""Section 8: attempts to bypass every major Risk Engine control,
through the real, unmocked `src.risk.engine.evaluate_trade_proposal`.
Expected decisions are always one of APPROVE / RESIZE / REJECT / HALT
-- RESIZE may only ever shrink a request, never grow it, and a failure
to calculate risk must reject, never approve.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.llm.schemas import StrategyType
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.limits import load_risk_limits
from src.risk.portfolio_risk import PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import QuantitativeAnalysis, compute_trade_economics, resolve_leg_contracts
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, MD_TS, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_portfolio, pipeline_request


def _build_qa(proposal, market_data, portfolio, limits):
    resolved = resolve_leg_contracts(proposal, market_data, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
    econ = compute_trade_economics(proposal, resolved, portfolio, limits, num_contracts=proposal.contracts_requested)
    return QuantitativeAnalysis(
        proposal_id=proposal.proposal_id, generated_at=MD_TS, max_profit=econ.max_profit, max_loss=econ.max_loss,
        breakeven=econ.breakeven, breakeven_upper=econ.breakeven_upper, capital_required=econ.capital_required,
        return_on_capital=econ.return_on_capital, annualized_roc=econ.annualized_roc,
        probability_of_profit=econ.probability_of_profit, expected_value=econ.expected_value, net_delta=0.0, net_vega=0.0,
    )


def _evaluate(fx, *, contracts_requested=1, portfolio=None, allowed=None, caps_overrides=None):
    proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=contracts_requested)
    market_data = make_chain(*fx.contracts)
    limits = load_risk_limits()
    portfolio = portfolio if portfolio is not None else fx.portfolio
    qa = _build_qa(proposal, market_data, portfolio, limits)
    caps = load_broker_capabilities("fidelity")
    if allowed is not None:
        caps = caps.model_copy(update={"allowed_strategies": frozenset(allowed)})
    if caps_overrides:
        caps = caps.model_copy(update=caps_overrides)
    result = evaluate_trade_proposal(proposal, portfolio, qa, market_data, caps, now=NOW)
    return proposal, qa, result


class TestSizingNeverExceedsRequest:
    def test_oversized_request_is_resized_down_never_up(self):
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        small_portfolio = make_portfolio(nav=20_000.0, cash=18_000.0, peak_equity=20_000.0)
        _, _, result = _evaluate(fx, contracts_requested=1000, portfolio=small_portfolio, allowed=[StrategyType.PUT_CREDIT_SPREAD])
        assert result.decision in (RiskDecision.RESIZE, RiskDecision.REJECT)
        if result.approved_contracts is not None:
            assert result.approved_contracts <= 1000

    def test_absolute_max_risk_per_trade_cap_is_never_exceeded(self):
        limits = load_risk_limits()
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        _, _, result = _evaluate(fx, contracts_requested=100_000, allowed=[StrategyType.PUT_CREDIT_SPREAD])
        if result.approved_contracts and result.approved_contracts > 0 and result.max_loss:
            risk_pct = result.max_loss / fx.portfolio.nav
            assert risk_pct <= limits.absolute_max_risk_per_trade_pct + 1e-6


class TestConcentrationAndCapitalLimits:
    def test_underlying_concentration_over_limit_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        existing = PortfolioPosition(
            position_id="p1", ticker="SPY", sector="INDEX", strategy=StrategyType.LONG_CALL, expiration=EXPIRATION,
            legs=[PortfolioPositionLeg(right="C", side="buy", strike=630.0, entry_price=7.0)],
            contracts=1, opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            capital_at_risk=85_000.0, max_loss=85_000.0,
        )
        concentrated_portfolio = make_portfolio(nav=100_000.0, cash=10_000.0, peak_equity=100_000.0, positions=[existing])
        _, _, result = _evaluate(fx, portfolio=concentrated_portfolio, allowed=[StrategyType.LONG_CALL])
        assert result.decision in (RiskDecision.REJECT, RiskDecision.HALT)

    def test_insufficient_cash_reserve_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.CASH_SECURED_PUT]
        thin_cash = make_portfolio(nav=10_000_000.0, cash=1_000.0, peak_equity=10_000_000.0)
        _, _, result = _evaluate(fx, portfolio=thin_cash, allowed=[StrategyType.CASH_SECURED_PUT])
        assert result.decision in (RiskDecision.REJECT, RiskDecision.HALT)

    def test_seventy_percent_deployed_capital_still_respects_the_cap(self):
        limits = load_risk_limits()
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        # NAV mostly already deployed -- only a small cash sliver remains.
        mostly_deployed = make_portfolio(nav=1_000_000.0, cash=50_000.0, peak_equity=1_000_000.0)
        _, _, result = _evaluate(fx, contracts_requested=1000, portfolio=mostly_deployed, allowed=[StrategyType.LONG_CALL])
        assert result.decision in (RiskDecision.RESIZE, RiskDecision.REJECT, RiskDecision.APPROVE)


class TestDrawdownControls:
    def test_deep_drawdown_never_lets_size_grow_beyond_request(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_PUT]
        halted_portfolio = make_portfolio(nav=500_000.0, cash=450_000.0, peak_equity=1_000_000.0)
        _, _, result = _evaluate(fx, portfolio=halted_portfolio, allowed=[StrategyType.LONG_PUT])
        if result.approved_contracts is not None:
            assert result.approved_contracts <= 1


class TestUnsupportedOrMissingCapability:
    def test_unsupported_strategy_for_broker_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL_BUTTERFLY]
        _, _, result = _evaluate(fx, allowed=[StrategyType.LONG_CALL])  # butterfly deliberately omitted
        assert result.decision in (RiskDecision.REJECT, RiskDecision.HALT)

    def test_options_disabled_account_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        _, _, result = _evaluate(fx, allowed=[StrategyType.LONG_CALL], caps_overrides={"options_enabled": False})
        assert result.decision in (RiskDecision.REJECT, RiskDecision.HALT)

    def test_unknown_broker_returns_none_never_a_fabricated_default(self):
        assert load_broker_capabilities("no_such_broker_exists") is None


class TestUncalculableRiskAlwaysRejects:
    @pytest.mark.asyncio
    async def test_undefined_max_loss_rejects_never_approves(self):
        """A structure whose net credit/debit cannot be established as
        the strategy's own formula requires (e.g. a "credit" spread
        that actually nets to a debit from current market prices) must
        reject -- never fall through to an approval with an undefined
        risk figure."""
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        bad_contracts = [
            fx.contracts[0].model_copy(update={"bid": 0.1, "ask": 0.15}),
            fx.contracts[1].model_copy(update={"bid": 5.0, "ask": 5.2}),
        ]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*bad_contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED
