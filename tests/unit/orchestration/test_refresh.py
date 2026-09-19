"""Tests for src.orchestration.refresh (Step 12's pre-execution
refresh): "If price moved materially: REPRICE_REQUIRED.\""""
from __future__ import annotations

from src.orchestration.pipeline import default_quant_stage
from src.orchestration.refresh import refresh_and_finalize_fidelity_ticket
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.engine import evaluate_trade_proposal as risk_evaluate
from src.risk.limits import load_risk_limits
from tests.unit.orchestration.conftest import MD_TS, NOW, LONG_SYMBOL, SHORT_SYMBOL, make_market_data, make_portfolio, make_proposal


def _quant_stage(proposal, market_data, portfolio, limits):
    return default_quant_stage(proposal, market_data, portfolio, limits, now=NOW)


def _original_ticket():
    limits = load_risk_limits()
    portfolio = make_portfolio()
    proposal = make_proposal()
    manual_caps = load_broker_capabilities("fidelity")
    market_data = make_market_data()
    qa = _quant_stage(proposal, market_data, portfolio, limits)
    result = risk_evaluate(proposal, portfolio, qa, market_data, manual_caps, limits=limits, now=NOW)
    return result.fidelity_ticket, limits, portfolio, proposal, manual_caps


class TestWithinThreshold:
    def test_tiny_market_move_finalizes_ok(self):
        ticket, limits, portfolio, proposal, manual_caps = _original_ticket()
        fresh = make_market_data()
        contracts = [c.model_copy(update={"bid": c.bid - 0.01, "ask": c.ask - 0.01}) for c in fresh.contracts]
        fresh = fresh.model_copy(update={"contracts": contracts})

        result = refresh_and_finalize_fidelity_ticket(
            proposal=proposal, portfolio=portfolio, limits=limits, original_ticket=ticket,
            fresh_market_data=fresh, manual_broker_capabilities=manual_caps,
            quant_stage=_quant_stage, risk_engine_stage=risk_evaluate, now=NOW,
        )
        assert result.status == "OK"
        assert result.ticket is not None


class TestMaterialMove:
    def test_large_credit_move_triggers_reprice_required(self):
        ticket, limits, portfolio, proposal, manual_caps = _original_ticket()
        fresh = make_market_data()
        contracts = []
        for c in fresh.contracts:
            if c.option_symbol == SHORT_SYMBOL:
                contracts.append(c.model_copy(update={"bid": 2.20, "ask": 2.40, "last": 2.30}))
            else:
                contracts.append(c)
        fresh = fresh.model_copy(update={"contracts": contracts})

        result = refresh_and_finalize_fidelity_ticket(
            proposal=proposal, portfolio=portfolio, limits=limits, original_ticket=ticket,
            fresh_market_data=fresh, manual_broker_capabilities=manual_caps,
            quant_stage=_quant_stage, risk_engine_stage=risk_evaluate, now=NOW,
        )
        assert result.status == "REPRICE_REQUIRED"
        assert result.ticket is None

    def test_custom_tighter_threshold_triggers_sooner(self):
        ticket, limits, portfolio, proposal, manual_caps = _original_ticket()
        fresh = make_market_data()
        contracts = []
        for c in fresh.contracts:
            if c.option_symbol == SHORT_SYMBOL:
                contracts.append(c.model_copy(update={"bid": c.bid + 0.05, "ask": c.ask + 0.05}))
            else:
                contracts.append(c)
        fresh = fresh.model_copy(update={"contracts": contracts})

        result = refresh_and_finalize_fidelity_ticket(
            proposal=proposal, portfolio=portfolio, limits=limits, original_ticket=ticket,
            fresh_market_data=fresh, manual_broker_capabilities=manual_caps,
            quant_stage=_quant_stage, risk_engine_stage=risk_evaluate, now=NOW,
            max_credit_move_pct=0.01, max_max_loss_move_pct=0.01,
        )
        assert result.status == "REPRICE_REQUIRED"


class TestRefreshRejection:
    def test_stale_fresh_market_data_is_rejected_not_finalized(self):
        from datetime import timedelta

        ticket, limits, portfolio, proposal, manual_caps = _original_ticket()
        stale = make_market_data(timestamp=NOW + timedelta(hours=2))
        contracts = [c.model_copy(update={"timestamp": NOW - timedelta(hours=2)}) for c in stale.contracts]
        stale = stale.model_copy(update={"contracts": contracts, "underlying": stale.underlying.model_copy(update={"timestamp": NOW - timedelta(hours=2)})})

        result = refresh_and_finalize_fidelity_ticket(
            proposal=proposal, portfolio=portfolio, limits=limits, original_ticket=ticket,
            fresh_market_data=stale, manual_broker_capabilities=manual_caps,
            quant_stage=_quant_stage, risk_engine_stage=risk_evaluate, now=NOW,
        )
        assert result.status == "REJECTED"
        assert result.ticket is None

    def test_quant_stage_failure_is_rejected(self):
        ticket, limits, portfolio, proposal, manual_caps = _original_ticket()

        def _broken_quant_stage(*args, **kwargs):
            raise RuntimeError("simulated quant failure")

        result = refresh_and_finalize_fidelity_ticket(
            proposal=proposal, portfolio=portfolio, limits=limits, original_ticket=ticket,
            fresh_market_data=make_market_data(), manual_broker_capabilities=manual_caps,
            quant_stage=_broken_quant_stage, risk_engine_stage=risk_evaluate, now=NOW,
        )
        assert result.status == "REJECTED"
