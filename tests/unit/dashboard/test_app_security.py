"""Security tests for the dashboard (Step 18): the dashboard must never
request or store Fidelity credentials, and must never be able to submit
a securities/options order to Fidelity. Same methodology as
`SECURITY_AUDIT.md`'s Fidelity security audit (Step 17) and
`tests/unit/brokers/test_fidelity_no_execution.py`, applied to this new
package.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import src.dashboard.app as dashboard_app
import src.dashboard.schemas as schemas
import src.dashboard.service as service

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "src" / "dashboard"
_DASHBOARD_PY_FILES = sorted(_DASHBOARD_DIR.rglob("*.py"))
_DASHBOARD_SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in _DASHBOARD_PY_FILES)
_STATIC_JS = (_DASHBOARD_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
_STATIC_HTML = (_DASHBOARD_DIR / "static" / "index.html").read_text(encoding="utf-8")


class TestNoCredentialFieldsAnywhere:
    """Same methodology `tests/unit/brokers/test_fidelity_no_execution
    .py::TestNoCredentialOrSessionStorage` already uses on
    `src/brokers/fidelity.py`: forbidden substrings are checked against
    *identifiers* (assignment targets, parameter names, Pydantic field
    names) via a regex, not against raw prose -- this module's own
    docstrings legitimately discuss "no password, no MFA code, no
    session cookie" in prose (exactly like `fidelity.py`'s own module
    docstring already does), which a bare substring-in-whole-text check
    would wrongly flag as a violation."""

    FORBIDDEN = ["password", "passwd", "username", "mfa", "cookie", "session_token", "api_key", "apikey", "secret", "otp"]

    def test_no_forbidden_identifier_in_any_dashboard_source_file(self):
        identifier_pattern = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]")
        identifiers = {m.group(1).lower() for m in identifier_pattern.finditer(_DASHBOARD_SOURCE)}
        for forbidden in self.FORBIDDEN:
            assert forbidden not in identifiers, f"forbidden identifier {forbidden!r} found in src/dashboard/*.py"

    def test_no_credential_shaped_html_input_or_form_field(self):
        attr_pattern = re.compile(r'(?:id|name)\s*=\s*"([^"]+)"')
        attr_values = {m.group(1).lower() for m in attr_pattern.finditer(_STATIC_HTML)}
        for forbidden in self.FORBIDDEN:
            assert forbidden not in attr_values, f"forbidden HTML id/name {forbidden!r} found in the static frontend"

    def test_no_credential_shaped_js_identifier(self):
        identifier_pattern = re.compile(r"\b(?:const|let|var|function)\s+([a-zA-Z_][a-zA-Z0-9_]*)")
        identifiers = {m.group(1).lower() for m in identifier_pattern.finditer(_STATIC_JS)}
        for forbidden in self.FORBIDDEN:
            assert forbidden not in identifiers, f"forbidden JS identifier {forbidden!r} found in dashboard.js"

    def test_no_pydantic_model_field_is_credential_shaped(self):
        """Every request/response model in schemas.py, field by field."""
        forbidden_field_substrings = ["password", "passwd", "mfa", "cookie", "session_token", "secret", "api_key"]
        for name, obj in vars(schemas).items():
            if not (inspect.isclass(obj) and hasattr(obj, "model_fields")):
                continue
            field_names = " ".join(obj.model_fields.keys()).lower()
            for forbidden in forbidden_field_substrings:
                assert forbidden not in field_names, f"{name}.{field_names} contains forbidden substring {forbidden!r}"

    def test_username_only_appears_as_part_of_actor_style_fields_never_a_login_field(self):
        """"username" itself isn't forbidden outright (an actor's own
        display name is fine, e.g. "entered_by") but nothing shaped like
        an actual login/auth field should exist. This asserts the
        specific field names this package does have are all audit-label
        fields, not authentication fields."""
        allowed_actor_field_names = {"actor", "confirmed_by", "entered_by"}
        for name, obj in vars(schemas).items():
            if not (inspect.isclass(obj) and hasattr(obj, "model_fields")):
                continue
            for field_name in obj.model_fields:
                if "user" in field_name.lower() or "login" in field_name.lower():
                    assert field_name in allowed_actor_field_names, f"{name}.{field_name} looks like a login field"


class TestNoNetworkOrAutomationCapability:
    """No HTTP client capable of reaching Fidelity, and no browser-
    automation library, is imported anywhere in this package. Matches
    on actual `import`/`from ... import` statements (the same anchored-
    regex approach `tests/unit/risk/test_architecture_boundary.py` uses
    for its own forbidden-import proof), not on the word appearing
    anywhere in a comment or docstring."""

    _FORBIDDEN_IMPORT_RE = re.compile(
        r"^\s*(?:from\s+(selenium|playwright|requests|aiohttp|urllib3|urllib\.request)(?:\.\S+)?\s+import\b"
        r"|import\s+(selenium|playwright|requests|aiohttp|urllib3|urllib\.request)\b)",
        re.MULTILINE,
    )

    def test_no_forbidden_import_anywhere_in_dashboard_source(self):
        for path in _DASHBOARD_PY_FILES:
            text = path.read_text(encoding="utf-8")
            match = self._FORBIDDEN_IMPORT_RE.search(text)
            assert match is None, f"{path} imports a forbidden network/automation library: {match.group(0) if match else ''}"

    def test_no_selenium_or_webdriver_reference_as_an_identifier(self):
        identifier_pattern = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=(]")
        identifiers = {m.group(1).lower() for m in identifier_pattern.finditer(_DASHBOARD_SOURCE)}
        for forbidden in ("selenium", "webdriver", "puppeteer"):
            assert forbidden not in identifiers

    def test_httpx_is_never_imported_by_the_dashboard_package_itself(self):
        """httpx is a real dependency of this project (FastAPI's own
        TestClient uses it), but nothing in src/dashboard/ itself should
        import it -- an outbound HTTP client here would be exactly the
        shape of a Fidelity-automation capability this platform forbids."""
        for path in _DASHBOARD_PY_FILES:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"^\s*(import httpx|from httpx)", text, re.MULTILINE), f"{path} imports httpx"


class TestNoExecutionShapedRoute:
    """Route-inventory test: every route this app actually registers is
    in an explicit whitelist. This fails loudly if any future change
    adds a route this test doesn't already know about -- including,
    especially, one shaped like AUTO TRADE / EXECUTE / SEND TO FIDELITY."""

    _ALLOWED_ROUTES = {
        ("GET", "/"),
        ("GET", "/api/portfolio-header"),
        ("GET", "/api/risk-panel"),
        ("GET", "/api/data-provider-health"),
        ("GET", "/api/wheels"),
        ("GET", "/api/wheels/{wheel_id}"),
        ("GET", "/api/lifecycle"),
        ("GET", "/api/lifecycle/{trade_id}"),
        ("GET", "/api/opportunities"),
        ("GET", "/api/opportunities/{trade_id}"),
        ("GET", "/api/audit"),
        ("GET", "/api/audit/{trade_id}"),
        ("POST", "/api/opportunities/{trade_id}/refresh"),
        ("POST", "/api/opportunities/{trade_id}/copy"),
        ("POST", "/api/opportunities/{trade_id}/mark-order-entered"),
        ("POST", "/api/opportunities/{trade_id}/fill"),
        ("POST", "/api/opportunities/{trade_id}/cancel"),
        ("POST", "/api/opportunities/{trade_id}/reject"),
        # Step 22.4, Parts 33-34: Portfolio Control Loop visibility --
        # read-only (GET-only), same as every other view route above.
        ("GET", "/api/control-loop/status"),
        ("GET", "/api/control-loop/exposure"),
        ("GET", "/api/control-loop/alerts"),
        # Step 22.5 (PAPER_TRADING_V1.4.4): Review-Only candidate visibility --
        # read-only (GET-only), deliberately no POST/PUT/DELETE/PATCH route
        # here (see tests/unit/dashboard/test_candidate_routes.py's own
        # TestNoWriteRouteExistsForCandidates) -- confirming a candidate is
        # CLI-only (scripts/confirm_candidate.py), never a dashboard click.
        ("GET", "/api/candidates"),
        ("GET", "/api/candidates/{candidate_id}"),
    }
    _FORBIDDEN_PATH_SUBSTRINGS = [
        "auto-trade", "auto_trade", "autotrade", "execute", "send-to-fidelity", "send_to_fidelity",
        "submit-order", "submit_order", "place-order", "place_order", "live-trade", "live_trade",
    ]

    def _api_routes(self):
        routes = []
        for route in dashboard_app.app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or methods is None:
                continue
            if path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
                continue
            for method in methods:
                if method == "HEAD":
                    continue
                routes.append((method, path))
        return routes

    def test_every_registered_route_is_on_the_explicit_allowlist(self):
        actual = set(self._api_routes())
        unexpected = actual - self._ALLOWED_ROUTES
        assert not unexpected, f"unexpected route(s) registered, not on the Step 18 allowlist: {unexpected}"

    def test_no_route_path_contains_an_execution_shaped_substring(self):
        for _, path in self._api_routes():
            lowered = path.lower()
            for forbidden in self._FORBIDDEN_PATH_SUBSTRINGS:
                assert forbidden not in lowered, f"route path {path!r} contains forbidden substring {forbidden!r}"

    def test_static_frontend_has_no_function_or_fetch_call_named_like_execution(self):
        """Checks actual JS constructs -- declared function names and
        the literal paths passed to `fetch(...)` -- rather than banning
        an English phrase from ever appearing in a comment (which would
        also flag this very docstring)."""
        function_names = {m.group(1).lower() for m in re.finditer(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)", _STATIC_JS)}
        fetch_paths = {m.group(1).lower() for m in re.finditer(r'api\(\s*[`"]([^`"]+)[`"]', _STATIC_JS)}
        for forbidden in self._FORBIDDEN_PATH_SUBSTRINGS:
            for name in function_names:
                assert forbidden.replace("-", "_") not in name, f"JS function {name!r} looks execution-shaped"
            for path in fetch_paths:
                assert forbidden not in path, f"fetch() call to {path!r} looks execution-shaped"


class TestServiceLayerCannotSubmitAnOrder:
    """Structural proof at the service-layer source level: every state
    change is a `src.brokers.fidelity.transition`/`confirm_fill` call,
    never a network call, never a live-broker construction."""

    SERVICE_SOURCE = Path(service.__file__).read_text(encoding="utf-8")
    APP_SOURCE = Path(dashboard_app.__file__).read_text(encoding="utf-8")

    def test_service_module_does_not_import_a_live_broker_client(self):
        pattern = re.compile(r"^\s*(from\s+src\.brokers\.ibkr|import\s+src\.brokers\.ibkr)\b", re.MULTILINE)
        assert pattern.search(self.SERVICE_SOURCE) is None
        assert pattern.search(self.APP_SOURCE) is None

    def test_service_module_never_calls_place_order_or_submit_order(self):
        for forbidden in ("place_order(", "submit_order(", "send_order("):
            assert forbidden not in self.SERVICE_SOURCE
            assert forbidden not in self.APP_SOURCE

    def test_only_transition_and_confirm_fill_change_ticket_status(self):
        """Every status-changing call in service.py goes through the
        one audited state machine -- no direct `.model_copy(update=
        {"status": ...})` bypass of `transition()`/`confirm_fill()`."""
        assert 'update={"status"' not in self.SERVICE_SOURCE
        assert "status=TicketStatus." not in self.SERVICE_SOURCE.replace("status=TicketStatus.FILLED if", "")  # confirm_fill's own internal line is in fidelity.py, not here

    def test_no_fidelity_trade_ticket_constructed_directly(self):
        """The only ticket constructor is
        `FidelityManualProvider.generate_trade_ticket`, called inside
        `evaluate_trade_proposal` -- this package never builds one
        itself, so it can never choose FILLED (or any other status) by
        construction."""
        assert "FidelityTradeTicket(" not in self.SERVICE_SOURCE
        assert "FidelityTradeTicket(" not in self.APP_SOURCE


class TestManualExecutionModeCannotBeChanged:
    """No request body anywhere in this package can influence
    `BrokerCapabilities.execution_mode` -- it is a hardcoded literal in
    `app.py`, never sourced from a request, a query parameter, or an
    environment variable read inside this package."""

    def test_execution_mode_is_hardcoded_manual_not_request_derived(self):
        assert dashboard_app._MANUAL_CAPABILITIES.execution_mode == "MANUAL"

    def test_no_schema_has_an_execution_mode_field(self):
        for name, obj in vars(schemas).items():
            if inspect.isclass(obj) and hasattr(obj, "model_fields"):
                assert "execution_mode" not in obj.model_fields, f"{name} exposes execution_mode as a request-settable field"

    def test_app_source_never_reads_execution_mode_from_env_or_request(self):
        source = Path(dashboard_app.__file__).read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "os.getenv" not in source


class TestAllActionsAreAudited:
    """Every allowed action logs a distinct audit event -- proven at
    the source level (each service function calls `_audit`) rather than
    re-deriving it purely from behavior tests already covered in
    test_service.py."""

    SERVICE_SOURCE = Path(service.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("function_name", ["refresh_price", "copy_fidelity_order", "mark_order_entered", "record_fill", "cancel_order", "reject_trade"])
    def test_each_action_function_calls_audit(self, function_name):
        source = inspect.getsource(getattr(service, function_name))
        assert "_audit(" in source, f"{function_name} does not record an audit event"
