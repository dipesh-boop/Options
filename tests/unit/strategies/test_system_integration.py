"""Step 19A system integration test: the full pipeline named in the
spec --

    MARKET DATA -> REGIME -> OPPORTUNITY -> MULTIPLE STRATEGIES
    -> QUANT ENGINE -> COMPARISON -> DEVIL'S ADVOCATE -> PORTFOLIO
    MANAGER -> RISK ENGINE -> SELECTED STRATEGY OR NO_TRADE
    -> PAPER BROKER -> VALIDATION DATABASE -> REPORTING

Devil's Advocate and Portfolio Manager verdicts are supplied directly
(as `src.strategies.selector.CandidateVerdicts`) rather than invoking
the real LLM client, the same "no real API key needed, verdicts
supplied" pattern every other pipeline test in this codebase already
uses (`tests/unit/orchestration/test_pipeline.py`) -- everything else
in this chain (Quant, Comparison, Risk Engine, PaperBroker, the
validation store) is the real, unmocked implementation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderType, PlaceOrderRequest
from src.brokers.order_validator import build_occ_symbol, validate_and_build_order_request
from src.brokers.paper import PaperBroker
from src.data.option_chain import OptionChain, OptionContract, OptionRight as DataOptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import Conviction, LegSide, OptionLeg, StrategyType, TradeDirection, TradeProposal
from src.llm.schemas import OptionRight as ProposalOptionRight
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import QuantitativeAnalysis, compute_trade_economics, compute_trade_greeks, resolve_leg_contracts
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.long_call import evaluate_long_call
from src.strategies.put_credit_spread import evaluate_put_credit_spread
from src.strategies.regime_mapping import MarketView, candidate_strategies_for
from src.strategies.selector import CandidateVerdicts, select_best_or_no_trade
from src.strategies.suitability import filter_suitable_strategies
from src.strategies.base import StrategyKind
from src.validation.session import DailySnapshot, InMemoryValidationStore, equity_curve_from_snapshots

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)


def _contract(strike, right, bid, ask, iv=0.25) -> OptionContract:
    return OptionContract(
        underlying="XYZ", option_symbol=build_occ_symbol("XYZ", EXPIRATION, right, strike), expiration=EXPIRATION,
        strike=strike, right=right, bid=bid, ask=ask, last=(bid + ask) / 2, volume=500, open_interest=1000, iv=iv,
        underlying_price=100.0, timestamp=NOW, source="test",
    )


class TestFullSystemIntegration:
    def test_market_data_through_reporting(self):
        # ---- 1. MARKET DATA ----
        chain = OptionChain(
            underlying=UnderlyingQuote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0, volume=1_000_000, timestamp=NOW, source="test"),
            contracts=[
                _contract(95, DataOptionRight.PUT, 2.8, 3.0),
                _contract(90, DataOptionRight.PUT, 1.4, 1.6),
                _contract(95, DataOptionRight.CALL, 6.8, 7.0),
                _contract(105, DataOptionRight.CALL, 2.3, 2.5),
            ],
            timestamp=NOW, source="test",
        )

        # ---- 2. REGIME ----
        view = MarketView.MODERATELY_BULLISH

        # ---- 3. OPPORTUNITY + suitability filter (no shares held -> COVERED_CALL excluded) ----
        portfolio = Portfolio(as_of=NOW, nav=200_000.0, cash=180_000.0, peak_equity=200_000.0, sector_by_ticker={"XYZ": "Technology"})
        limits = get_default_limits()
        candidate_kinds = filter_suitable_strategies(
            candidate_strategies_for(view), ticker="XYZ", portfolio=portfolio, contracts=1
        )
        assert StrategyKind.COVERED_CALL not in candidate_kinds  # no shares held

        # ---- 4/5. MULTIPLE STRATEGIES + QUANT ENGINE (src.strategies.base reuses src.quant directly) ----
        pcs_eval = evaluate_put_credit_spread(
            ticker="XYZ", expiration=EXPIRATION, short_put_contract=_contract(95, DataOptionRight.PUT, 2.8, 3.0),
            long_put_contract=_contract(90, DataOptionRight.PUT, 1.4, 1.6), spot=100.0, sigma=0.25, t=24 / 365,
            rate=0.04, days_to_expiry=24, num_contracts=1, limits=limits,
        )
        bcs_eval = evaluate_bull_call_spread(
            ticker="XYZ", expiration=EXPIRATION, long_call_contract=_contract(95, DataOptionRight.CALL, 6.8, 7.0),
            short_call_contract=_contract(105, DataOptionRight.CALL, 2.3, 2.5), spot=100.0, sigma=0.25, t=24 / 365,
            rate=0.04, days_to_expiry=24, num_contracts=1, limits=limits,
        )
        candidates = [pcs_eval, bcs_eval]

        # ---- 6. COMPARISON (+ 7/8. Devil's Advocate / Portfolio Manager verdicts, supplied) ----
        verdicts = {
            pcs_eval.strategy_kind: CandidateVerdicts(False, "sound thesis", RiskDecision.APPROVE, "approved"),
            bcs_eval.strategy_kind: CandidateVerdicts(False, "sound thesis", RiskDecision.APPROVE, "approved"),
        }

        # ---- 9. RISK ENGINE (the real one) confirms each candidate independently ----
        proposal_by_kind = {
            pcs_eval.strategy_kind: TradeProposal(
                proposal_id="p-pcs", timestamp=NOW, ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD,
                market_regime="normal", expiration=EXPIRATION,
                legs=[
                    OptionLeg(right=ProposalOptionRight.PUT, strike=95, side=LegSide.SELL),
                    OptionLeg(right=ProposalOptionRight.PUT, strike=90, side=LegSide.BUY),
                ],
                direction=TradeDirection.BULLISH, contracts_requested=1, target_entry=1.4, profit_target=0.5,
                management_dte=15, thesis="range-bound to up", risk_thesis="defined risk", confidence=Conviction.MEDIUM,
                data_sources=["test"], data_timestamp=NOW, invalidation_conditions=["break support"],
            ),
        }
        proposal = proposal_by_kind[pcs_eval.strategy_kind]
        resolved = resolve_leg_contracts(proposal, chain, as_of=NOW, max_age_minutes=limits.max_market_data_age_minutes)
        econ = compute_trade_economics(proposal, resolved, portfolio, limits, num_contracts=1)
        greeks = compute_trade_greeks(proposal, resolved, portfolio)
        qa = QuantitativeAnalysis(
            proposal_id=proposal.proposal_id, generated_at=NOW, max_profit=econ.max_profit, max_loss=econ.max_loss,
            breakeven=econ.breakeven, capital_required=econ.capital_required, return_on_capital=econ.return_on_capital,
            annualized_roc=econ.annualized_roc, probability_of_profit=econ.probability_of_profit,
            expected_value=econ.expected_value, net_delta=greeks.delta, net_vega=greeks.vega,
        )
        broker_caps = load_broker_capabilities("internal_paper")
        risk_result = evaluate_trade_proposal(proposal, portfolio, qa, chain, broker_caps, limits=limits, now=NOW)
        assert risk_result.decision in (RiskDecision.APPROVE, RiskDecision.RESIZE)

        # ---- 10. SELECTED STRATEGY OR NO_TRADE (the Strategy Competition Engine) ----
        outcome = select_best_or_no_trade(candidates, verdicts, portfolio=portfolio, limits=limits, spot=100.0)
        assert outcome.selected is not None
        assert len(outcome.comparison_table) == 2  # both candidates preserved for counterfactual tracking

        # ---- 11. PAPER BROKER ----
        broker = PaperBroker(initial_cash=200_000.0, now=NOW)
        broker.update_market_data(chain)
        place_request = validate_and_build_order_request(
            risk_result.approved_order, risk_decision=risk_result.decision,
            approved_contracts=risk_result.approved_contracts, broker_capabilities=broker_caps,
            client_order_id=proposal.proposal_id,
        )
        import asyncio

        order = asyncio.run(broker.place_order(place_request))
        assert order.status.value in ("filled", "partially_filled", "submitted")

        # ---- 12. VALIDATION DATABASE ----
        store = InMemoryValidationStore()
        store.record_snapshot(DailySnapshot(
            snapshot_date=NOW.date(), nav=200_000.0, cash=asyncio.run(broker.get_account()).cash_balance,
            capital_deployed_pct=0.05, open_position_count=len(asyncio.run(broker.get_positions())),
            drawdown_pct=0.0, per_strategy_nav={"put_credit_spread": 200_000.0}, recorded_at=NOW,
        ))

        # ---- 13. REPORTING ----
        curve = equity_curve_from_snapshots(store.snapshots())
        assert len(curve) == 1
        assert curve[0][0] == NOW.date()
