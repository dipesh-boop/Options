"""Step 22.4A Part 12: hostile-audit-style negative-capability tests for
the outer Portfolio Control Loop orchestrator (`src.portfolio.orchestrator`)
and its dashboard projection (`src.dashboard.control_loop_projection`).

Mirrors `tests/acceptance/test_tradier_market_data_only.py`'s own
methodology, applied to the new orchestration/dashboard-initialization
surface Step 22.4A adds: the orchestrator must remain a thin coordinator
over the existing, unmodified Risk Engine, Lifecycle Engine, and
Fidelity manual-ticket state machine -- never a second implementation of
any of them, never able to infer a fill from a Risk approval, never able
to start live trading or the 90-day validation cohort, and the dashboard
projection this step adds must remain strictly read-only.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

_ORCHESTRATOR_SRC = (SRC_ROOT / "portfolio" / "orchestrator.py").read_text()
_PROJECTION_SRC = (SRC_ROOT / "dashboard" / "control_loop_projection.py").read_text()
_SMOKE_SCRIPT_SRC = (REPO_ROOT / "scripts" / "smoke_tradier_market_data.py").read_text()

_ORDER_SHAPED_METHOD_PATTERN = re.compile(
    r"\b(place_order|submit_order|cancel_order|modify_order|amend_order|"
    r"submit_trade|execute_trade|place_trade|send_order|preview_order|replace_order)\b", re.IGNORECASE,
)
_TRADIER_ORDER_ENDPOINT_PATTERN = re.compile(r"/v1/accounts/[^\"'\s]*/orders", re.IGNORECASE)


class TestTradierRemainsMarketDataOnlyThroughTheOrchestrator:
    def test_orchestrator_never_references_an_order_shaped_method_name(self):
        assert not _ORDER_SHAPED_METHOD_PATTERN.search(_ORCHESTRATOR_SRC), (
            "src/portfolio/orchestrator.py references an order-shaped method name"
        )

    def test_orchestrator_never_references_a_tradier_order_endpoint(self):
        assert not _TRADIER_ORDER_ENDPOINT_PATTERN.search(_ORCHESTRATOR_SRC)

    def test_smoke_test_script_never_references_an_order_shaped_method_name(self):
        assert not _ORDER_SHAPED_METHOD_PATTERN.search(_SMOKE_SCRIPT_SRC), (
            "scripts/smoke_tradier_market_data.py references an order-shaped method name"
        )

    def test_smoke_test_script_never_calls_a_post_put_delete_http_verb(self):
        for forbidden in ("_http_client.post(", "_http_client.put(", "_http_client.delete(", "_http_client.patch("):
            assert forbidden not in _SMOKE_SCRIPT_SRC

    def test_smoke_test_script_never_logs_the_token_value(self):
        forbidden_call_pattern = re.compile(r"print\([^)]*\bconfig\.token\b", re.IGNORECASE)
        assert not forbidden_call_pattern.search(_SMOKE_SCRIPT_SRC), (
            "scripts/smoke_tradier_market_data.py appears to print the raw token value"
        )

    def test_no_tradier_order_shaped_class_referenced_by_the_orchestrator_or_smoke_script(self):
        pattern = re.compile(r"Tradier(Broker|Order(Client|Provider)?|ExecutionProvider)\b")
        assert not pattern.search(_ORCHESTRATOR_SRC)
        assert not pattern.search(_SMOKE_SCRIPT_SRC)


class TestOrchestratorCannotBypassRiskOrLifecycle:
    def test_orchestrator_never_imports_a_live_trading_client(self):
        import_pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
        assert not import_pattern.search(_ORCHESTRATOR_SRC)

    def test_orchestrator_never_calls_an_order_submission_method(self):
        order_method_pattern = re.compile(
            r"\b(place_order|submit_order|cancel_order|modify_order|amend_order|"
            r"preview_order|replace_order|submit_trade|execute_trade|place_trade|send_order)\s*\(",
            re.IGNORECASE,
        )
        assert not order_method_pattern.search(_ORCHESTRATOR_SRC)

    def test_orchestrator_calls_the_real_risk_engine_never_a_second_implementation(self):
        assert "scan_and_rank_opportunities" in _ORCHESTRATOR_SRC
        # `evaluate_trade_proposal` (the one deterministic Risk Engine) is
        # reached only indirectly, through the unmodified
        # `scan_and_rank_opportunities` -- the orchestrator itself never
        # imports `src.risk.engine` directly (a bare code-level import
        # statement, not the module's own docstring prose, which freely
        # names `src.risk.engine.evaluate_trade_proposal` when explaining
        # this exact indirection), so it cannot construct a second,
        # parallel call to it with different arguments.
        import_pattern = re.compile(r"^\s*(from|import)\s+src\.risk\.engine\b", re.MULTILINE)
        assert not import_pattern.search(_ORCHESTRATOR_SRC)

    def test_orchestrator_never_reimplements_the_lifecycle_engine(self):
        # `run_control_cycle` (imported, unmodified) is the only caller
        # of `evaluate_position` -- the orchestrator itself never imports
        # `src.lifecycle.engine` directly.
        assert "from src.lifecycle.engine import" not in _ORCHESTRATOR_SRC
        assert "evaluate_position(" not in _ORCHESTRATOR_SRC

    def test_orchestrator_never_calls_run_control_cycle_more_than_once_per_cycle(self):
        assert _ORCHESTRATOR_SRC.count("run_control_cycle(") == 1

    def test_orchestrator_never_infers_a_fill(self):
        # A Risk approval or an opportunity-scan `best` candidate must
        # never be silently treated as a fill -- the orchestrator never
        # imports/calls `confirm_fill` (the one function anywhere in this
        # codebase that can transition a ticket to FILLED/PARTIALLY_FILLED).
        assert "confirm_fill" not in _ORCHESTRATOR_SRC
        assert "record_confirmed_fill" not in _ORCHESTRATOR_SRC
        assert "TicketStatus.FILLED" not in _ORCHESTRATOR_SRC

    def test_orchestrator_never_imports_fidelity_ticket_submission_path(self):
        # `monitor_pending_tickets`/`TicketMonitorConfig` (read-only
        # monitoring, already Risk-approved-only by construction) are the
        # only Fidelity-shaped imports -- never `ApprovedOrder`,
        # `FidelityManualProvider.generate_trade_ticket`, or anything else
        # that could originate a NEW ticket from this module.
        assert "generate_trade_ticket" not in _ORCHESTRATOR_SRC
        assert "ApprovedOrder" not in _ORCHESTRATOR_SRC
        assert "FidelityManualProvider" not in _ORCHESTRATOR_SRC

    def test_orchestrator_never_starts_the_validation_cohort(self):
        assert "src.validation.session" not in _ORCHESTRATOR_SRC
        assert "start_cohort" not in _ORCHESTRATOR_SRC
        assert "validation_cohort_started" not in _ORCHESTRATOR_SRC


class TestOrchestratorNeverOutranksExistingPositionRiskMonitoring:
    """Part 14's explicit freeze requirement: new-opportunity scanning
    cannot outrank existing-position risk monitoring."""

    def test_run_control_cycle_call_is_never_conditioned_on_rate_limit_state(self):
        # `run_control_cycle` must be called unconditionally, never inside
        # an `if may_proceed(...)`-style gate the way the two optional
        # stages are.
        call_index = _ORCHESTRATOR_SRC.index("control_result = run_control_cycle(control_inputs)")
        preceding = _ORCHESTRATOR_SRC[:call_index]
        # the two optional-stage helper calls must appear strictly before
        # the unconditional run_control_cycle call, and neither
        # `_run_ticket_monitor_stage` nor `_run_opportunity_scan_stage`
        # wraps the run_control_cycle call itself.
        assert "_run_ticket_monitor_stage(inputs)" in preceding
        assert "_run_opportunity_scan_stage(inputs)" in preceding

    def test_opportunity_scan_priority_is_strictly_lower_than_ticket_monitor_priority(self):
        from src.data.rate_limiter import RateLimitPriority
        from src.portfolio.orchestrator import _OPPORTUNITY_SCAN_PRIORITY, _TICKET_MONITOR_PRIORITY

        assert _OPPORTUNITY_SCAN_PRIORITY > _TICKET_MONITOR_PRIORITY
        assert _TICKET_MONITOR_PRIORITY > RateLimitPriority.P0_POSITION_RISK


class TestDashboardProjectionIsReadOnly:
    def test_projection_module_never_imports_a_mutating_fidelity_or_risk_function(self):
        forbidden = ("confirm_fill", "transition(", "evaluate_trade_proposal", "evaluate_position")
        for name in forbidden:
            assert name not in _PROJECTION_SRC, f"{name!r} unexpectedly present in control_loop_projection.py"

    def test_projection_module_has_exactly_one_public_function(self):
        import src.dashboard.control_loop_projection as mod

        public = [n for n in dir(mod) if not n.startswith("_") and callable(getattr(mod, n)) and getattr(getattr(mod, n), "__module__", "") == mod.__name__]
        assert public == ["load_latest_control_loop_state"]

    def test_no_route_in_the_dashboard_app_can_resolve_a_control_loop_alert(self):
        import src.dashboard.app as dashboard_app

        for route in dashboard_app.app.routes:
            path = getattr(route, "path", "")
            if "control-loop" in path and "alert" in path:
                methods = getattr(route, "methods", set())
                assert methods <= {"GET", "HEAD"}, f"{path} exposes a non-read method: {methods}"


class TestFidelityRemainsManualAndUntouchedByThisStep:
    def test_brokers_yaml_fidelity_execution_mode_still_manual(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert brokers_cfg["fidelity"]["execution_mode"] == "MANUAL"

    def test_brokers_yaml_does_not_list_tradier_as_an_execution_broker(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert "tradier" not in {k.lower() for k in brokers_cfg}

    def test_no_automatic_order_entry_path_anywhere_in_src_portfolio(self):
        # `tests/acceptance/test_tradier_market_data_only.py`'s own
        # `test_control_loop_never_imports_a_tradier_execution_path`
        # already proves no forbidden Tradier trading class name or
        # `place_order`/`submit_order` substring exists anywhere in
        # `src/portfolio/` -- this check additionally covers the fuller
        # order-shaped-method vocabulary (`cancel_order`, `preview_order`,
        # `replace_order`, etc.).
        for path in (SRC_ROOT / "portfolio").rglob("*.py"):
            text = path.read_text()
            assert not _ORDER_SHAPED_METHOD_PATTERN.search(text), f"{path} references an order-shaped method name"


class TestLiveTradingAndValidationCannotBeStartedFromThisSurface:
    def test_control_loop_package_never_imports_a_live_trading_client(self):
        import_pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
        for path in (SRC_ROOT / "portfolio").rglob("*.py"):
            assert not import_pattern.search(path.read_text()), f"{path} imports a live trading client"

    def test_control_loop_package_never_imports_the_validation_cohort_start_path(self):
        for path in (SRC_ROOT / "portfolio").rglob("*.py"):
            text = path.read_text()
            assert "src.validation.session" not in text
            assert "start_cohort" not in text

    def test_dashboard_control_loop_files_never_import_the_validation_cohort_start_path(self):
        for name in ("app.py", "control_loop_projection.py", "models.py", "schemas.py"):
            text = (SRC_ROOT / "dashboard" / name).read_text()
            assert "src.validation.session" not in text
            assert "start_cohort" not in text
