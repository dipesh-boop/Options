"""Step 22.8 (PAPER_TRADING_V1.4.7) acceptance/unit tests: the operator-
status dashboard route, the safe validation-cycle trigger route, and the
structural/secrecy/local-only guarantees the task spec's items 11-15
require.

Item 11 (dashboard/startup cannot bypass Review-Only workflow) is
proven two ways: structurally (no route anywhere accepts a candidate id
for a write, `TestNoWriteRouteExistsForCandidates`/the allowlist test
in `test_app_security.py` already cover this and are unmodified) and
functionally, end-to-end, below -- driving a real Risk-approved
candidate scenario through `POST /api/validation-cycle/run` and
asserting the outcome is identical to the CLI path already proven safe
by `tests/acceptance/test_review_only_daily_cycle.py`: exactly one
AWAITING_HUMAN candidate, zero PaperBroker orders.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.dashboard.validation_ops as validation_ops
from src.dashboard.app import app
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    environment,  # noqa: F401 -- pytest fixture, imported for reuse
)
from tests.acceptance.test_review_only_daily_cycle import (
    operations_config_module as _ops_config_module,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _executable_lines(shell_script_text: str) -> str:
    """Strips full-line `#` comments before a structural scan, so a
    launcher's own explanatory comments (which necessarily name the
    exact things they promise NOT to do) can never produce a false
    positive -- mirrors `src.validation.freeze`'s own docstring-
    stripping precedent for the identical class of problem."""
    return "\n".join(line for line in shell_script_text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(autouse=True)
def _reset_cached_runner_module():
    """`validation_ops._load_runner_module` caches its loaded module at
    process scope -- reset it around every test in this file so one
    test's monkeypatched `get_configured_market_data_provider` can
    never leak into another."""
    validation_ops._runner_module = None
    yield
    validation_ops._runner_module = None


@pytest.fixture
def _isolated_operational_db(tmp_path, monkeypatch):
    """A handful of tests below exercise `build_operator_status`/the
    dashboard's real routes with no seeded `environment` fixture (that
    fixture's own elaborate seeded-cohort setup is unnecessary for an
    "unconfigured"/"mock provider" scenario). Without this, `load_operations_config()`/
    `load_validation_config()` fall through to `config/operations.yaml`'s/
    `config/validation.yaml`'s own real default (`data/options_agent.db`),
    and every `SqliteXStore(...)` construction downstream would silently
    create/touch that literal repository-root path the moment it connects
    -- CLAUDE.md and every prior freeze step are explicit that no test may
    ever touch the real operational database. Every DB-path env override
    this codebase already defines is pointed at this test's own `tmp_path`
    instead, the same isolation `tests/acceptance/test_review_only_daily_cycle
    .py`'s `environment` fixture already establishes for the tests that use it."""
    monkeypatch.setenv("OPTIONS_AGENT_VALIDATION_DB_PATH", str(tmp_path / "validation.db"))
    monkeypatch.setenv("OPTIONS_AGENT_ACCOUNT_STATE_DB_PATH", str(tmp_path / "ops.db"))
    monkeypatch.setenv("OPTIONS_AGENT_CONTROL_LOOP_DB_PATH", str(tmp_path / "ops.db"))
    monkeypatch.setenv("OPTIONS_AGENT_LIFECYCLE_DB_PATH", str(tmp_path / "ops.db"))
    monkeypatch.setenv("OPTIONS_AGENT_CANDIDATE_REVIEW_DB_PATH", str(tmp_path / "ops.db"))


class TestOperatorStatusRoute:
    def test_returns_200_and_never_raises_when_unconfigured(self, _isolated_operational_db):
        client = TestClient(app)
        resp = client.get("/api/operator-status")
        assert resp.status_code == 200

    def test_response_never_contains_a_configured_secret(self, _isolated_operational_db, monkeypatch):
        """Item 13: secrets are not returned by dashboard/API/status
        output. Sets a real-looking Tradier token in the environment,
        then asserts it never appears anywhere in the JSON response --
        not verbatim, not as a substring."""
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        secret_token = "sk-live-super-secret-tradier-token-should-never-leak"  # noqa: S105 -- test fixture value, not a real credential
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", secret_token)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-also-must-never-leak")

        client = TestClient(app)
        resp = client.get("/api/operator-status")
        assert resp.status_code == 200
        body_text = resp.text
        assert secret_token not in body_text
        assert "sk-ant-also-must-never-leak" not in body_text

    def test_reports_provider_readiness_without_crashing_on_mock(self, _isolated_operational_db, monkeypatch):
        monkeypatch.delenv("OPTIONS_AGENT_DATA_PROVIDER", raising=False)
        client = TestClient(app)
        resp = client.get("/api/operator-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"]["provider"] == "mock"
        assert body["provider"]["ready"] is False


class TestValidationCycleRunRoute:
    def test_accepts_no_body_and_never_places_an_order_when_provider_is_mock(self, _isolated_operational_db, monkeypatch):
        """A click against a misconfigured (mock) environment must fail
        the exact same provider preflight the CLI already enforces --
        never silently substitute a provider, never mutate anything."""
        monkeypatch.delenv("OPTIONS_AGENT_DATA_PROVIDER", raising=False)
        client = TestClient(app)
        resp = client.post("/api/validation-cycle/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "place_order(" not in body["log"]  # the call shape, not the explanatory prose that mentions it by name

    def test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome(self, environment, monkeypatch):
        """Item 11, functional proof: driving a real Risk-approvable
        scenario through the dashboard's own POST route produces the
        exact same safe outcome
        `tests/acceptance/test_review_only_daily_cycle.py` already
        proves for the CLI -- exactly one AWAITING_HUMAN candidate,
        zero PaperBroker orders. This route can never confirm that
        candidate; no code path here imports
        `src.review.confirmation.confirm_candidate`."""
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

        # Load the runner module once here so its own
        # get_configured_market_data_provider can be monkeypatched to a
        # fully offline fake, exactly like the CLI acceptance fixture
        # does for the script module directly.
        runner_module = validation_ops._load_runner_module()
        monkeypatch.setattr(runner_module, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        # Step 2: this test proves dashboard-vs-CLI parity for the
        # Review-Only workflow, never the market-hours gate itself --
        # bypassed so it never depends on the real wall-clock time the
        # suite happens to run at.
        monkeypatch.setattr(
            runner_module,
            "evaluate_validation_cycle_eligibility",
            lambda now, **kw: ValidationCycleEligibility(
                as_of=now, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
                regular_session_open=None, regular_session_close=None,
                validation_cycle_allowed=True, block_reason=None,
            ),
        )

        client = TestClient(app)
        resp = client.post("/api/validation-cycle/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "place_order(" not in body["log"]  # the call shape, not the explanatory prose that mentions it by name

        ops = _ops_config_module.load_operations_config()
        from src.brokers.base import SqliteIdempotencyStore
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(awaiting) == 1
        assert awaiting[0].status.value == "awaiting_human"

        idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
        assert idempotency.all() == []  # no order was ever placed -- Review-Only preserved


class TestNoDashboardRouteCanConfirmACandidate:
    """Item 11, structural half: grep the live route table itself for
    anything path- or shape-like a candidate confirmation."""

    def test_no_route_accepts_a_candidate_id_for_a_mutating_action(self):
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path is None:
                continue
            if "{candidate_id}" in path:
                mutating = methods - {"GET", "HEAD"}
                assert not mutating, f"unexpected mutating method(s) on {path!r}: {mutating}"

    def test_confirmation_module_never_imported_by_the_dashboard_app(self):
        # Scoped to actual import statements, not prose -- both files'
        # own docstrings/comments explain, by name, that confirmation
        # stays CLI-only, which would otherwise trip a naive whole-file
        # substring scan for the very string that explains its absence.
        import_pattern = re.compile(r"^\s*(?:import|from)\s+\S*(?:review\.confirmation|confirm_candidate)\S*", re.MULTILINE)
        app_source = (REPO_ROOT / "src" / "dashboard" / "app.py").read_text(encoding="utf-8")
        validation_ops_source = (REPO_ROOT / "src" / "dashboard" / "validation_ops.py").read_text(encoding="utf-8")
        for source in (app_source, validation_ops_source):
            assert not import_pattern.search(source)


class TestLocalOnlyDefault:
    """Item 12: local dashboard default remains 127.0.0.1."""

    def test_start_sh_defaults_to_127_0_0_1(self):
        start_sh = (REPO_ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
        assert 'HOST="${OPTIONS_AGENT_DASHBOARD_HOST:-127.0.0.1}"' in start_sh
        assert "0.0.0.0" not in start_sh

    def test_launcher_command_does_not_hardcode_a_public_bind_address(self):
        launcher = (REPO_ROOT / "Options Trading Dashboard.command").read_text(encoding="utf-8")
        assert "0.0.0.0" not in _executable_lines(launcher)

    def test_no_source_file_hardcodes_binding_to_all_interfaces(self):
        # A broad, deliberately simple net: nothing under src/dashboard/
        # or scripts/ should ever bind uvicorn to 0.0.0.0.
        for path in list((REPO_ROOT / "src" / "dashboard").glob("*.py")) + list((REPO_ROOT / "scripts").glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            assert "0.0.0.0" not in text, f"{path} unexpectedly references 0.0.0.0"


class TestMacLauncherNeverAutomatesUnsafeActions:
    """Items 14-15: the launcher never runs the validation cycle or
    confirms a candidate automatically -- a structural, text-based
    check on the launcher's own source, matching this codebase's
    established `_verify_*` structural-check style
    (`src.validation.freeze`)."""

    @pytest.fixture
    def launcher_text(self) -> str:
        return (REPO_ROOT / "Options Trading Dashboard.command").read_text(encoding="utf-8")

    @pytest.fixture
    def launcher_code(self, launcher_text) -> str:
        return _executable_lines(launcher_text)

    def test_launcher_never_invokes_the_validation_cycle_script_or_make_target(self, launcher_code):
        forbidden = [
            "run_validation_cycle.py",
            "run_validation_cycle.sh",
            "validate-cycle",
            "validate-preflight",
        ]
        for token in forbidden:
            assert token not in launcher_code, f"launcher unexpectedly references {token!r} in executable code"

    def test_launcher_never_invokes_confirm_candidate(self, launcher_code):
        assert "confirm_candidate" not in launcher_code
        assert "confirm-candidate" not in launcher_code

    def test_launcher_only_ever_execs_start_sh(self, launcher_text):
        exec_lines = [line for line in launcher_text.splitlines() if re.match(r"^\s*exec\s", line)]
        assert exec_lines == ["exec ./scripts/start.sh"]

    def test_launcher_is_repo_relative_not_a_hardcoded_machine_path(self, launcher_text):
        assert "BASH_SOURCE" in launcher_text
        assert "/home/" not in launcher_text
        assert "/Users/" not in launcher_text


class TestRiskDataWiringObservability:
    """PAPER_TRADING_V1.5.3, Step 3: the operator-status route must
    report whether sector/correlation risk-data wiring is active for
    the current cohort's own config -- never silently omit it."""

    def test_reports_installed_inactive_by_default(self, environment):
        from datetime import datetime, timezone

        status = validation_ops.build_operator_status(now=datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc))
        assert status.configured is True
        assert status.risk_data_wiring_status == "INSTALLED_INACTIVE"

    def test_reports_installed_active_once_the_operator_config_enables_it(self, environment, monkeypatch):
        import yaml

        ops_yaml = environment / "operations.yaml"
        data = yaml.safe_load(ops_yaml.read_text())
        data["risk_data_wiring"]["enabled"] = True
        ops_yaml.write_text(yaml.safe_dump(data))

        from datetime import datetime, timezone

        status = validation_ops.build_operator_status(now=datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc))
        assert status.risk_data_wiring_status == "INSTALLED_ACTIVE"

    def test_unconfigured_degraded_status_never_fabricates_a_wiring_status(self, _isolated_operational_db):
        status = validation_ops.build_operator_status()
        if not status.configured:
            assert status.risk_data_wiring_status is None
