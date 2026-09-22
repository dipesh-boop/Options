"""Part 18: the 11-level action-precedence hierarchy -- a higher
category always wins over a lower one, regardless of the lower
category's own mandatory flag, and within a category a mandatory
finding always wins over a merely advisory one."""
from __future__ import annotations

from src.lifecycle.precedence import resolve_action
from src.lifecycle.state import PositionLifecycleState as S
from src.lifecycle.triggers import TriggerFinding


def _f(name, category, target_state, mandatory=False, reason="x") -> TriggerFinding:
    return TriggerFinding(trigger_name=name, category=category, target_state=target_state, mandatory=mandatory, reason=reason)


class TestNoFindingsIsHold:
    def test_empty_findings_resolves_to_hold(self):
        r = resolve_action([], risk_halt_active=False)
        assert r.category == "hold" and r.target_state == S.ACTIVE and not r.mandatory


class TestDataSafetyAlwaysWins:
    def test_data_safety_beats_risk_halt(self):
        findings = [_f("quote_stale", "system_data_safety", S.DATA_INSUFFICIENT, mandatory=True)]
        r = resolve_action(findings, risk_halt_active=True)
        assert r.category == "system_data_safety" and r.target_state == S.DATA_INSUFFICIENT

    def test_data_safety_beats_every_other_category(self):
        findings = [
            _f("quote_stale", "system_data_safety", S.DATA_INSUFFICIENT, mandatory=True),
            _f("max_loss_pct", "hard_loss_exposure", S.LOSS_THRESHOLD_REACHED, mandatory=True),
            _f("profit_target_pct", "profit_target", S.PROFIT_TARGET_REACHED),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "system_data_safety"

    def test_multiple_data_safety_findings_merge_reasons(self):
        findings = [
            _f("delta", "system_data_safety", S.DATA_INSUFFICIENT, mandatory=True, reason="delta missing"),
            _f("dte", "system_data_safety", S.DATA_INSUFFICIENT, mandatory=True, reason="dte missing"),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert "delta missing" in r.reason and "dte missing" in r.reason
        assert set(r.winning_trigger_names) == {"delta", "dte"}


class TestRiskHaltOutranksEverythingElse:
    def test_risk_halt_beats_hard_loss(self):
        findings = [_f("max_loss_pct", "hard_loss_exposure", S.LOSS_THRESHOLD_REACHED, mandatory=True)]
        r = resolve_action(findings, risk_halt_active=True)
        assert r.category == "risk_halt" and r.target_state == S.RISK_EXIT_REQUIRED and r.mandatory

    def test_risk_halt_beats_profit_target(self):
        findings = [_f("profit_target_pct", "profit_target", S.PROFIT_TARGET_REACHED)]
        r = resolve_action(findings, risk_halt_active=True)
        assert r.category == "risk_halt"

    def test_no_risk_halt_falls_through(self):
        findings = [_f("profit_target_pct", "profit_target", S.PROFIT_TARGET_REACHED)]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "profit_target"


class TestCategoryOrdering:
    def test_hard_loss_beats_assignment(self):
        findings = [
            _f("assign", "assignment_expiration", S.ADJUSTMENT_CANDIDATE, mandatory=True),
            _f("loss", "hard_loss_exposure", S.LOSS_THRESHOLD_REACHED, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "hard_loss_exposure"

    def test_assignment_beats_event_risk(self):
        findings = [
            _f("earnings", "event_risk", S.TIME_EXIT_TRIGGERED, mandatory=True),
            _f("assign", "assignment_expiration", S.ADJUSTMENT_CANDIDATE, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "assignment_expiration"

    def test_event_risk_beats_liquidity(self):
        findings = [
            _f("liq", "liquidity_risk", S.ADJUSTMENT_CANDIDATE),
            _f("earnings", "event_risk", S.TIME_EXIT_TRIGGERED, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "event_risk"

    def test_liquidity_beats_time_exit(self):
        findings = [
            _f("dte", "time_exit", S.TIME_EXIT_TRIGGERED, mandatory=True),
            _f("liq", "liquidity_risk", S.ADJUSTMENT_CANDIDATE),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "liquidity_risk"

    def test_time_exit_beats_profit_target(self):
        findings = [
            _f("profit", "profit_target", S.PROFIT_TARGET_REACHED),
            _f("dte", "time_exit", S.TIME_EXIT_TRIGGERED, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "time_exit"

    def test_profit_target_beats_delta_volatility_review(self):
        findings = [
            _f("delta", "delta_volatility_review", S.DELTA_TRIGGERED),
            _f("profit", "profit_target", S.PROFIT_TARGET_REACHED),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.category == "profit_target"


class TestWithinCategoryMandatoryWins:
    def test_mandatory_delta_close_beats_advisory_review_in_same_category(self):
        findings = [
            _f("delta_threshold", "delta_volatility_review", S.DELTA_TRIGGERED, mandatory=False),
            _f("delta_close_threshold", "delta_volatility_review", S.DELTA_TRIGGERED, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert r.mandatory and r.winning_trigger_names == ("delta_close_threshold",)

    def test_all_findings_retained_regardless_of_winner(self):
        findings = [
            _f("profit", "profit_target", S.PROFIT_TARGET_REACHED),
            _f("dte", "time_exit", S.TIME_EXIT_TRIGGERED, mandatory=True),
        ]
        r = resolve_action(findings, risk_halt_active=False)
        assert len(r.all_findings) == 2
