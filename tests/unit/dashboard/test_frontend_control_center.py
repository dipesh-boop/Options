"""Step 22.9 (PAPER_TRADING_V1.4.8): tests for the Daily Validation
Control frontend that finally wires the dashboard UI to V1.4.7's
backend operator APIs (`GET /api/operator-status`,
`POST /api/validation-cycle/run`). No new route is added this step --
`tests/unit/dashboard/test_app_security.py`'s existing route allowlist
and `test_operator_status.py`'s existing route-level tests are
unmodified and continue to cover the backend contract; the tests here
are scoped to the new frontend files
(`src/dashboard/static/operator_control.js`,
`src/dashboard/static/dashboard.js`, `src/dashboard/static/index.html`)
and to the small, additive, read-only fields
`build_operator_status()` gained (cohort start/end date, preferred
trade target, unresolved alerts).

Covers spec items 1-20 not already covered by
`tests/unit/dashboard/test_operator_status.py` (still unmodified) or
`tests/frontend/operator_control.test.js` (the pure cycle-state/
button-state/run-guard logic, run via Node -- item 21 below shells out
to that suite so it participates in the ordinary `pytest` full run).
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.dashboard.validation_ops as validation_ops
from src.dashboard.app import app
from src.dashboard.validation_ops import OperatorStatusView, build_operator_status
from src.portfolio.alerts import AlertSeverity, ControlLoopAlert, ControlLoopAlertType
from src.portfolio.persistence import SqliteControlLoopStore
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    environment,  # noqa: F401 -- pytest fixture, imported for reuse
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_STATIC_DIR = REPO_ROOT / "src" / "dashboard" / "static"


def _read(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    """Extracts one top-level `function NAME(...) { ... }` body by brace
    matching, so a check can be scoped to "does THIS function do X"
    rather than "does the whole file mention X anywhere" -- the same
    class of precision `src.validation.freeze`'s own `_extract_function_body`
    already establishes for Python source, applied here to JS."""
    marker = re.search(rf"function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{", source)
    assert marker is not None, f"no function named {function_name!r} found"
    start = marker.end() - 1  # position of the opening brace
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces while extracting {function_name!r}")


@pytest.fixture(autouse=True)
def _reset_cached_runner_module():
    validation_ops._runner_module = None
    yield
    validation_ops._runner_module = None


# ------------------------------------------------------------- item 1, 2, 18


class TestFrontendWiredToOperatorStatus:
    def test_index_html_loads_operator_control_before_dashboard_js(self):
        html = _read("index.html")
        oc_idx = html.index('src="/static/operator_control.js"')
        db_idx = html.index('src="/static/dashboard.js"')
        assert oc_idx < db_idx, "operator_control.js must load before dashboard.js so its globals exist first"

    def test_index_html_declares_the_control_center_section_and_run_button(self):
        html = _read("index.html")
        assert 'id="control-center"' in html
        assert 'id="run-validation-btn"' in html
        assert "onclick=\"onRunDailyValidation()\"" in html
        for field_id in ("cc-cohort", "cc-market-data", "cc-today-cycle", "cc-portfolio", "cc-progress", "cc-review", "cc-alerts"):
            assert f'id="{field_id}"' in html

    def test_dashboard_js_calls_operator_status_on_load(self):
        js = _read("dashboard.js")
        assert '"/api/operator-status"' in js
        assert "loadOperatorStatus()" in js

    def test_render_control_center_reads_every_documented_status_field(self):
        """Item 2: software/cohort/provider/today status renders --
        checked structurally by confirming the render functions actually
        reference the OperatorStatusView fields they're documented to
        show, not just that some function with a plausible name exists."""
        js = _read("dashboard.js")
        for field in (
            "cohort_id", "cohort_started_at", "cohort_planned_end_date",
            "provider", "today_cycle_ran", "today_cycle_degraded", "today_cycle_halted",
            "nav", "cash", "open_position_count", "drawdown_pct",
            "awaiting_review_count", "awaiting_review_candidate_ids", "alerts",
        ):
            assert field in js, f"dashboard.js never references OperatorStatusView field {field!r}"

    def test_operator_status_view_is_backward_compatible_with_v1_4_7(self):
        """Item 18: existing dashboard APIs remain compatible -- every
        field V1.4.7's OperatorStatusView had is still present with the
        same name; this step only ever ADDS fields."""
        v1_4_7_fields = {
            "configured", "detail", "cohort_id", "nav", "cash", "open_position_count",
            "drawdown_pct", "last_snapshot_date", "today_cycle_id", "today_cycle_ran",
            "today_cycle_degraded", "today_cycle_halted", "today_cycle_errors", "provider",
            "awaiting_review_count", "awaiting_review_candidate_ids", "validation_duration_days",
            "validation_days_recorded", "validation_minimum_completed_trades", "validation_completed_trades",
        }
        assert v1_4_7_fields.issubset(set(OperatorStatusView.model_fields.keys()))


# --------------------------------------------------------------------- item 3


class TestNoSecretRendersInFrontend:
    def test_operator_status_route_still_never_leaks_a_configured_secret(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        secret_token = "sk-live-super-secret-tradier-token-should-never-leak"  # noqa: S105
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", secret_token)
        client = TestClient(app)
        resp = client.get("/api/operator-status")
        assert resp.status_code == 200
        assert secret_token not in resp.text

    def test_no_frontend_file_references_a_secret_shaped_env_var_name(self):
        forbidden = ["TRADIER_TOKEN", "ANTHROPIC_API_KEY", "API_SECRET", "ALPACA_SECRET"]
        for name in ("dashboard.js", "operator_control.js", "index.html"):
            text = _read(name)
            for token in forbidden:
                assert token not in text, f"{name} unexpectedly references {token!r}"


# ------------------------------------------------------------- items 4, 5, 6


class TestRunEndpointUsedOnlyByExplicitTrigger:
    def test_only_one_reference_to_the_validation_cycle_run_endpoint(self):
        """Item 4: the Run button uses only the existing POST endpoint,
        and nothing else in the file duplicates or re-derives that
        call."""
        js = _read("dashboard.js")
        assert js.count('"/api/validation-cycle/run"') == 1

    def test_load_all_never_posts_the_validation_cycle(self):
        """Item 5: page load never POSTs validation."""
        js = _read("dashboard.js")
        load_all_body = _function_body(js, "loadAll")
        assert "/api/validation-cycle/run" not in load_all_body
        assert 'method: "POST"' not in load_all_body

    def test_load_operator_status_is_get_only(self):
        """Item 6: refresh/polling never POSTs validation."""
        js = _read("dashboard.js")
        body = _function_body(js, "loadOperatorStatus")
        assert "/api/validation-cycle/run" not in body
        assert "POST" not in body

    def test_poll_interval_only_ever_calls_load_all(self):
        js = _read("dashboard.js")
        assert re.search(r"setInterval\(\s*loadAll\s*,", js) is not None

    def test_only_on_run_daily_validation_posts_to_the_endpoint(self):
        js = _read("dashboard.js")
        body = _function_body(js, "onRunDailyValidation")
        assert '"/api/validation-cycle/run"' in body
        assert 'method: "POST"' in body


# ------------------------------------------------------------------ item 7


class TestDoubleClickProtection:
    def test_on_run_daily_validation_checks_the_guard_before_posting(self):
        js = _read("dashboard.js")
        body = _function_body(js, "onRunDailyValidation")
        assert "runGuard.isInFlight()" in body
        assert "runGuard.beginRun()" in body
        assert "runGuard.endRun()" in body
        # The guard check must come before the fetch, not after.
        assert body.index("runGuard.beginRun()") < body.index('"/api/validation-cycle/run"')

    def test_run_guard_is_a_single_shared_instance(self):
        js = _read("dashboard.js")
        assert "const runGuard = createRunGuard();" in js
        # createRunGuard is not called a second time anywhere else in the file.
        assert js.count("createRunGuard()") == 1


# -------------------------------------------------------------- items 8-10
# (button-disabled-by-state logic itself is exercised directly, per
# state, by tests/frontend/operator_control.test.js's runButtonState
# tests -- these confirm dashboard.js actually WIRES that pure function
# into the real button rather than reimplementing the decision inline.)


class TestRunButtonWiredToPureLogic:
    def test_render_cc_run_button_uses_run_button_state(self):
        js = _read("dashboard.js")
        body = _function_body(js, "renderCcRunButton")
        assert "runButtonState(" in body
        assert "btn.disabled = disabled" in body

    def test_render_control_center_always_calls_render_cc_run_button(self):
        js = _read("dashboard.js")
        body = _function_body(js, "renderControlCenter")
        assert "renderCcRunButton(" in body


# ----------------------------------------------------------- items 11, 12


class TestNoConfirmationControlAnywhereInTheFrontend:
    def test_no_button_or_element_confirms_a_candidate(self):
        html = _read("index.html")
        js = _read("dashboard.js")
        oc = _read("operator_control.js")
        forbidden_ui = ["Confirm Trade", "Approve Trade", "Place Order", "Execute Trade", "Confirm Candidate"]
        for token in forbidden_ui:
            assert token not in html
            assert token not in js
            assert token not in oc

    def test_candidate_review_card_never_offers_an_action(self):
        js = _read("dashboard.js")
        body = _function_body(js, "renderCcReview")
        assert "onclick" not in body
        assert "<button" not in body
        assert "No position has been opened" in body

    def test_no_frontend_file_calls_or_references_confirm_candidate(self):
        """Item 12, matching test_operator_status.py's own
        import-statement-scoped precedent, adapted for frontend files:
        no `fetch`/`api()` call path anywhere names a confirmation
        action."""
        call_pattern = re.compile(r'api\(\s*[`"]([^`"]+)[`"]')
        for name in ("dashboard.js", "operator_control.js"):
            text = _read(name)
            for path in call_pattern.findall(text):
                assert "confirm" not in path.lower(), f"{name} calls a confirmation-shaped path: {path!r}"


# --------------------------------------------------------------- item 13, 14


class TestNoBrokerageOrExecutionCallsInFrontend:
    _FORBIDDEN_PATH_SUBSTRINGS = [
        "auto-trade", "auto_trade", "autotrade", "execute", "send-to-fidelity", "send_to_fidelity",
        "submit-order", "submit_order", "place-order", "place_order", "live-trade", "live_trade", "confirm",
    ]

    def test_no_place_order_call_shape_anywhere_in_frontend(self):
        for name in ("dashboard.js", "operator_control.js"):
            assert "place_order(" not in _read(name)

    def test_no_fetch_call_targets_a_brokerage_execution_shaped_path(self):
        call_pattern = re.compile(r'api\(\s*[`"]([^`"]+)[`"]')
        for name in ("dashboard.js", "operator_control.js"):
            text = _read(name)
            for path in call_pattern.findall(text):
                lowered = path.lower()
                for forbidden in self._FORBIDDEN_PATH_SUBSTRINGS:
                    if forbidden == "confirm":
                        continue  # covered precisely by TestNoConfirmationControlAnywhereInTheFrontend
                    assert forbidden not in lowered, f"{name} calls forbidden-shaped path {path!r}"


# ------------------------------------------------------------------ item 15


class TestEmptyStatesRenderCleanly:
    def test_wheels_and_lifecycle_show_a_helpful_empty_state_not_a_blank_box(self):
        js = _read("dashboard.js")
        wheels_body = _function_body(js, "renderWheels")
        lifecycle_body = _function_body(js, "renderLifecycle")
        assert "No wheel research available" in wheels_body
        assert "No active positions" in lifecycle_body
        # Neither function merely hides the section anymore.
        assert "display" not in wheels_body
        assert "display" not in lifecycle_body

    def test_control_center_blocks_have_an_empty_state_for_missing_data(self):
        js = _read("dashboard.js")
        for fn in ("renderCcCohort", "renderCcMarketData", "renderCcPortfolio", "renderCcProgress", "renderCcReview", "renderCcAlerts"):
            body = _function_body(js, fn)
            assert 'class="empty"' in body, f"{fn} has no empty-state branch"


# --------------------------------------------------------------- item 16


class TestHistoricalAlertsNeverBlockAHealthyCurrentCycle:
    def test_cycle_state_functions_never_read_the_alerts_field(self):
        """The frontend's cycle-state/button-state decisions are
        structurally independent of `status.alerts` -- proven directly
        against the pure-logic source (also exercised behaviorally by
        tests/frontend/operator_control.test.js)."""
        oc = _read("operator_control.js")
        cycle_state_body = _function_body(oc, "deriveCycleState")
        button_state_body = _function_body(oc, "runButtonState")
        assert "alerts" not in cycle_state_body
        assert "alerts" not in button_state_body

    def test_build_operator_status_excludes_a_resolved_historical_alert(self, environment, tmp_path):
        """A Sep-23-style resolved/degraded alert must not appear in
        `alerts` (which only ever surfaces `all_unresolved_alerts()`),
        and must not affect today's `today_cycle_degraded`/
        `today_cycle_halted`, which come only from today's own cycle
        record."""
        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        old_alert = ControlLoopAlert(
            alert_id="2026-09-23-degraded-mock",
            scope="global",
            alert_type=ControlLoopAlertType.PROVIDER_OUTAGE,
            severity=AlertSeverity.WARNING,
            reason="2026-09-23 cycle ran against mock/degraded data",
            created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            resolved=True,
            resolved_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        )
        control_loop_store.save_alert(old_alert)

        status = build_operator_status(now=datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc))
        assert status.alerts == ()

    def test_build_operator_status_surfaces_a_currently_unresolved_alert(self, environment, tmp_path):
        ops = _ops_config_module.load_operations_config()
        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        current_alert = ControlLoopAlert(
            alert_id="2026-09-24-current",
            scope="global",
            alert_type=ControlLoopAlertType.PROVIDER_OUTAGE,
            severity=AlertSeverity.CRITICAL,
            reason="today's cycle is degraded",
            created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            resolved=False,
        )
        control_loop_store.save_alert(current_alert)

        status = build_operator_status(now=datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc))
        assert len(status.alerts) == 1
        assert status.alerts[0].alert_id == "2026-09-24-current"
        assert status.alerts[0].resolved is False


# --------------------------------------------------------------- item 17


class TestLocalOnlyDefaultUnaffectedByFrontendChange:
    def test_no_new_frontend_file_hardcodes_a_public_bind_address(self):
        for name in ("dashboard.js", "operator_control.js", "index.html"):
            assert "0.0.0.0" not in _read(name)


# --------------------------------------------------------------- item 19


class TestValidationEndpointIdempotencyUnaffected:
    def test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        runner_module = validation_ops._load_runner_module()
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())

        client = TestClient(app)
        first = client.post("/api/validation-cycle/run")
        assert first.status_code == 200
        assert first.json()["success"] is True

        second = client.post("/api/validation-cycle/run")
        assert second.status_code == 200
        # The second call is the backend's own documented idempotent
        # no-op path -- still never places an order either way.
        assert "place_order(" not in second.json()["log"]

        ops = _ops_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(awaiting) == 1  # not duplicated by the second call


# --------------------------------------------------------------- item 21


class TestNodeFrontendSuitePasses:
    """Runs `node --test tests/frontend/` as part of the ordinary
    Python suite, so the pure cycle-state/button-state/run-guard logic
    (items 1, 3 partial, 8, 9, 10, 16 partial from the task's list) is
    verified on every `pytest` run without requiring a second, separate
    invocation. Skips (never fails) if Node isn't available in this
    environment -- the source-level tests above still cover the same
    contracts structurally either way."""

    def test_node_test_runner_passes(self):
        import shutil

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not available in this environment")
        result = subprocess.run(
            [node, "--test", "tests/frontend/operator_control.test.js"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"node --test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
