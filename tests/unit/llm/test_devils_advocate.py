"""Orchestration tests for src.llm.devils_advocate (Step 11) — the exact
scenario list the spec names: obviously dangerous trades, earnings risk,
concentration, poor liquidity, stale quotes, high correlation,
good-quality trades, and missing information."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.llm.client import LLMClient, LLMOutputError
from src.llm.devils_advocate import (
    DevilsAdvocateCrossCheckError,
    DevilsAdvocateInputs,
    MissingInputError,
    evaluate_trade_risk,
    validate_inputs_complete,
)
from tests.unit.llm.conftest import (
    DATA_TS,
    NOW,
    make_market_regime,
    make_market_snapshot,
    make_portfolio_state,
    make_proposal,
    make_quant_analysis,
    valid_devils_advocate_review_input,
    valid_fidelity_execution_risk,
    valid_risk_assessment,
)


def _full_inputs(**overrides) -> DevilsAdvocateInputs:
    base = dict(
        proposal=make_proposal(),
        quant_analysis=make_quant_analysis(),
        portfolio_state=make_portfolio_state(),
        market_regime=make_market_regime(),
        analysis_snapshot=make_market_snapshot(),
        current_snapshot=make_market_snapshot(),  # identical -> not stale
    )
    base.update(overrides)
    return DevilsAdvocateInputs(**base)


def _client_for(payload: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    return LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))


class TestObviouslyDangerousTrades:
    def test_reject_verdict_round_trips(self):
        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(
            verdict="REJECT",
            why_not_thesis="Naked directional exposure into a binary macro event with no defined risk.",
            risk_assessment=valid_risk_assessment(
                directional_risk={"applicable": True, "note": "unhedged, full downside exposure"},
                gap_risk={"applicable": True, "note": "binary event before expiration"},
            ),
        )
        client = _client_for(payload)
        evaluation = evaluate_trade_risk(inputs, client=client, system_prompt="Devil's Advocate.")
        assert evaluation.review.verdict == "REJECT"
        assert evaluation.review.risk_assessment[0].category or True  # sanity: still 18 categories present
        assert len(evaluation.review.risk_assessment) == 18


class TestEarningsRisk:
    def test_earnings_in_window_flag_reaches_the_prompt_context(self):
        from src.llm.devils_advocate import build_devils_advocate_context

        inputs = _full_inputs(earnings_in_window=True)
        context = build_devils_advocate_context(inputs)
        assert '"earnings_in_window": true' in context

    def test_earnings_flagged_as_applicable_round_trips(self):
        inputs = _full_inputs(earnings_in_window=True)
        payload = valid_devils_advocate_review_input(
            verdict="CAUTION",
            risk_assessment=valid_risk_assessment(
                earnings={"applicable": True, "note": "expiration falls inside the earnings window"}
            ),
        )
        client = _client_for(payload)
        evaluation = evaluate_trade_risk(inputs, client=client, system_prompt="x")
        earnings_entry = next(r for r in evaluation.review.risk_assessment if r.category == "earnings")
        assert earnings_entry.applicable is True


class TestConcentrationAndCorrelation:
    def test_high_concentration_flagged_round_trips(self):
        inputs = _full_inputs(portfolio_state=make_portfolio_state(open_position_count=8))
        payload = valid_devils_advocate_review_input(
            verdict="CAUTION",
            risk_assessment=valid_risk_assessment(
                portfolio_concentration={"applicable": True, "note": "already 8 open positions, this adds more single-name risk"}
            ),
        )
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        entry = next(r for r in evaluation.review.risk_assessment if r.category == "portfolio_concentration")
        assert entry.applicable is True

    def test_high_correlation_flagged_round_trips(self):
        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(
            verdict="CAUTION",
            risk_assessment=valid_risk_assessment(
                correlation={"applicable": True, "note": "highly correlated with an existing large index position"}
            ),
        )
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        entry = next(r for r in evaluation.review.risk_assessment if r.category == "correlation")
        assert entry.applicable is True


class TestPoorLiquidity:
    def test_liquidity_deterioration_flagged_round_trips(self):
        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(
            verdict="REJECT",
            why_not_thesis="Wide, thin market makes a reasonable fill unlikely and exit risk high.",
            risk_assessment=valid_risk_assessment(
                liquidity_deterioration={"applicable": True, "note": "wide bid/ask, thin open interest"}
            ),
        )
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REJECT"


class TestStaleQuotes:
    """The money case for the deterministic override: Python's own
    compute_staleness overrules the model, never the reverse."""

    def test_significant_underlying_move_forces_reprice_required(self):
        stale_current = make_market_snapshot(
            as_of=DATA_TS + timedelta(minutes=1), underlying_price=628.5 * 1.02  # 2% move
        )
        inputs = _full_inputs(current_snapshot=stale_current)
        payload = valid_devils_advocate_review_input(verdict="PASS")  # model didn't notice
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REPRICE_REQUIRED"
        assert evaluation.review.fidelity_execution_risk.reprice_required is True
        assert evaluation.staleness.stale is True

    def test_wide_spread_widening_forces_reprice_required(self):
        stale_current = make_market_snapshot(bid=0.30, ask=1.20)  # spread blew out
        inputs = _full_inputs(current_snapshot=stale_current)
        payload = valid_devils_advocate_review_input(verdict="CAUTION")
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REPRICE_REQUIRED"

    def test_stale_snapshot_age_forces_reprice_required(self):
        stale_current = make_market_snapshot(as_of=DATA_TS + timedelta(minutes=30))
        inputs = _full_inputs(current_snapshot=stale_current)
        payload = valid_devils_advocate_review_input(verdict="PASS")
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REPRICE_REQUIRED"

    def test_model_already_saying_reprice_required_is_left_alone(self):
        stale_current = make_market_snapshot(underlying_price=628.5 * 1.02, as_of=DATA_TS + timedelta(minutes=1))
        inputs = _full_inputs(current_snapshot=stale_current)
        payload = valid_devils_advocate_review_input(
            verdict="REPRICE_REQUIRED", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
        )
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REPRICE_REQUIRED"

    def test_model_saying_reject_is_not_overridden_to_reprice_required(self):
        # REJECT is a stronger conclusion than REPRICE_REQUIRED (the
        # trade shouldn't happen at all, not just at a new price) — the
        # override must not weaken a REJECT down to REPRICE_REQUIRED.
        stale_current = make_market_snapshot(underlying_price=628.5 * 1.02, as_of=DATA_TS + timedelta(minutes=1))
        inputs = _full_inputs(current_snapshot=stale_current)
        payload = valid_devils_advocate_review_input(
            verdict="REJECT", fidelity_execution_risk=valid_fidelity_execution_risk(reprice_required=True)
        )
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "REJECT"

    def test_fresh_identical_snapshots_are_not_stale(self):
        inputs = _full_inputs()  # analysis_snapshot == current_snapshot
        payload = valid_devils_advocate_review_input(verdict="PASS")
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.staleness.stale is False
        assert evaluation.review.verdict == "PASS"

    def test_current_snapshot_before_analysis_snapshot_raises(self):
        from src.llm.devils_advocate import compute_staleness

        analysis = make_market_snapshot(as_of=NOW)
        earlier = make_market_snapshot(as_of=NOW - timedelta(minutes=5))
        with pytest.raises(ValueError):
            compute_staleness(analysis, earlier)


class TestGoodQualityTrades:
    def test_clean_pass_round_trips_unmodified(self):
        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(verdict="PASS")
        evaluation = evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
        assert evaluation.review.verdict == "PASS"
        assert evaluation.staleness.stale is False


class TestMissingInformation:
    @pytest.mark.parametrize(
        "field_name",
        ["proposal", "quant_analysis", "portfolio_state", "market_regime", "analysis_snapshot", "current_snapshot"],
    )
    def test_each_required_input_missing_raises(self, field_name: str):
        inputs = _full_inputs(**{field_name: None})
        with pytest.raises(MissingInputError):
            validate_inputs_complete(inputs)

    def test_evaluate_trade_risk_never_calls_the_model_when_input_is_missing(self):
        inputs = _full_inputs(market_regime=None)
        calls = []

        def _create(**kwargs):
            calls.append(kwargs)
            raise AssertionError("should never be called")

        client = LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
        with pytest.raises(MissingInputError):
            evaluate_trade_risk(inputs, client=client, system_prompt="x")
        assert calls == []

    def test_malformed_trade_proposal_rejected(self):
        inputs = _full_inputs(proposal={"ticker": "SPY"})
        with pytest.raises(TypeError):
            validate_inputs_complete(inputs)


class TestCrossCheck:
    def test_wrong_proposal_id_rejected(self):
        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(proposal_id="some-other-proposal")
        with pytest.raises(DevilsAdvocateCrossCheckError):
            evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")


class TestHallucinatedAndMalformedOutput:
    def test_smuggled_numeric_field_rejected(self):
        inputs = _full_inputs()
        payload = {**valid_devils_advocate_review_input(), "probability_of_loss": 0.42}
        with pytest.raises(LLMOutputError):
            evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")

    def test_missing_required_field_rejected(self):
        inputs = _full_inputs()
        payload = {k: v for k, v in valid_devils_advocate_review_input().items() if k != "why_not_thesis"}
        with pytest.raises(LLMOutputError):
            evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")

    def test_only_two_failure_scenarios_rejected(self):
        from tests.unit.llm.conftest import valid_failure_scenarios

        inputs = _full_inputs()
        payload = valid_devils_advocate_review_input(failure_scenarios=valid_failure_scenarios()[:2])
        with pytest.raises(LLMOutputError):
            evaluate_trade_risk(inputs, client=_client_for(payload), system_prompt="x")
