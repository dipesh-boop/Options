"""Step 22.3 Part 28's security proofs: the lifecycle engine cannot
create an execution path. No live brokerage execution was added, no
Fidelity auto-execution, no Alpaca trading API, no browser automation,
no credential logging, no LLM Risk override, and no LLM direct state
mutation. Every check here is a structural, executable proof over
`src/lifecycle/`'s actual source -- never a comment asserting an
intention."""
from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.lifecycle import (
    adjustment,
    alerts,
    engine,
    excursion,
    fidelity_events,
    paper_events,
    persistence,
    policies_library,
    policy,
    precedence,
    rolling,
    snapshot,
    state,
    triggers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_SRC_DIR = REPO_ROOT / "src" / "lifecycle"
_DETERMINISTIC_MODULES = [
    adjustment, alerts, engine, excursion, persistence, policies_library, policy, precedence, rolling, snapshot, state, triggers,
]
_ALL_MODULES = _DETERMINISTIC_MODULES + [fidelity_events, paper_events]


def _lifecycle_source_files() -> list[Path]:
    return sorted(p for p in LIFECYCLE_SRC_DIR.glob("*.py") if p.name != "__pycache__")


class TestNoLiveBrokerageExecutionAnywhereInLifecycle:
    _FORBIDDEN_IMPORT_PATTERN = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
    # place_order is deliberately excluded: PaperBroker.place_order is
    # this platform's own internal simulator (src.brokers.paper),
    # called legitimately from src.lifecycle.paper_events -- the
    # live-shaped names below are ones no module in this codebase
    # defines for any real broker at all.
    _FORBIDDEN_METHOD_NAME_PATTERN = re.compile(
        r"\b(submit_order|send_order|execute_trade|submit_trade|place_trade)\s*\(", re.IGNORECASE
    )

    def test_no_live_trading_client_import_anywhere_in_lifecycle_package(self):
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            assert not self._FORBIDDEN_IMPORT_PATTERN.search(text), f"{path} imports a live trading client"

    def test_no_order_submission_method_name_called_anywhere(self):
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            for match in self._FORBIDDEN_METHOD_NAME_PATTERN.finditer(text):
                pytest.fail(f"{path} calls forbidden live-execution-shaped method: {match.group(0)!r}")

    def test_no_network_client_imports(self):
        forbidden = re.compile(r"^\s*(from|import)\s+(requests|httpx|aiohttp|urllib\.request|socket)\b", re.MULTILINE)
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text), f"{path} imports a network client -- src.lifecycle has no network client of any kind"

    def test_no_browser_automation_imports(self):
        forbidden = re.compile(r"^\s*(from|import)\s+(selenium|playwright|puppeteer)\b", re.MULTILINE)
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text), f"{path} imports a browser automation library"


class TestNoAlpacaTradingApi:
    def test_no_alpaca_import_anywhere(self):
        forbidden = re.compile(r"^\s*(from|import)\s+alpaca\b", re.MULTILINE)
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text), f"{path} imports alpaca -- Alpaca remains market-data-only elsewhere in this codebase, never here"


class TestNoCredentialLogging:
    _FORBIDDEN_FIELD_PATTERN = re.compile(r"\b(password|passwd|mfa_code|session_cookie|auth_token|api_key|api_secret)\b", re.IGNORECASE)

    def test_no_credential_shaped_field_anywhere_in_lifecycle_package(self):
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            match = self._FORBIDDEN_FIELD_PATTERN.search(text)
            assert match is None, f"{path} contains a credential-shaped identifier: {match.group(0)!r}"


class TestFidelityRemainsManualOnly:
    def test_fidelity_events_never_calls_confirm_fill_or_transition_itself(self):
        source = inspect.getsource(fidelity_events)
        assert "confirm_fill(" not in source

    def test_only_record_ticket_filled_ever_sets_the_filled_status(self):
        """LifecycleTicketStatus.FILLED appears exactly where it's
        supposed to: as record_ticket_filled's own target status. No
        other function (including build_closing_ticket, which must
        always start a ticket AWAITING_HUMAN) is allowed to construct
        or return an already-FILLED ticket."""
        source = inspect.getsource(fidelity_events)
        build_source = inspect.getsource(fidelity_events.build_closing_ticket)
        assert "LifecycleTicketStatus.FILLED" not in build_source
        assert source.count("LifecycleTicketStatus.FILLED") == 1  # only inside record_ticket_filled's update dict
        assert "LifecycleTicketStatus.FILLED" in inspect.getsource(fidelity_events.record_ticket_filled)

    def test_record_ticket_filled_requires_an_explicit_human_supplied_timestamp(self):
        sig = inspect.signature(fidelity_events.record_ticket_filled)
        assert "filled_at" in sig.parameters

    def test_render_closing_ticket_text_never_claims_submission(self):
        ticket = fidelity_events.build_closing_ticket(
            trade_id="T1", ticker="XYZ",
            legs=[fidelity_events.LifecycleClosingLegTicket(
                action=fidelity_events.LifecycleClosingAction.BUY_TO_CLOSE,
                put_call=__import__("src.data.option_chain", fromlist=["OptionRight"]).OptionRight.PUT,
                strike=95.0, expiration=date(2026, 2, 20), contracts=1,
            )],
            reason="test", now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        text = fidelity_events.render_closing_ticket_text(ticket)
        assert "No order has been submitted to any brokerage" in text


class TestPaperBrokerCloseNeverBypassesRiskForAnOpen:
    def test_paper_events_module_only_ever_closes_never_opens(self):
        """A roll/adjustment's new OPEN leg must go through the full
        Risk-Engine-gated pipeline (src.orchestration.pipeline) exactly
        like any other TradeProposal -- src.lifecycle.paper_events
        exposes no function that opens a new position."""
        public_functions = [name for name, obj in inspect.getmembers(paper_events, inspect.isfunction) if not name.startswith("_")]
        for name in public_functions:
            assert "open" not in name.lower(), f"paper_events.{name} appears to open a position -- opens must go through the Risk-gated pipeline"


class TestNoLlmRiskOverride:
    """CLAUDE.md: "No LLM ever computes an authoritative price, Greek,
    probability, or risk figure" and "No LLM output... can bypass" the
    Risk Engine. Proven the same way
    tests/unit/risk/test_architecture_boundary.py and
    tests/acceptance/test_wheel_security.py already prove it: none of
    the deterministic lifecycle modules import the LLM client or router."""

    def test_no_deterministic_lifecycle_module_imports_the_llm_client_or_router(self):
        for module in _DETERMINISTIC_MODULES:
            source = inspect.getsource(module)
            assert "src.llm.client" not in source, f"{module.__name__} imports the LLM client"
            assert "src.llm.router" not in source, f"{module.__name__} imports the LLM router"

    def test_evaluate_position_signature_carries_no_llm_verdict_parameter(self):
        sig = inspect.signature(engine.evaluate_position)
        for name in sig.parameters:
            assert "llm" not in name.lower()
            assert "advocate" not in name.lower()
            assert "portfolio_decision" not in name.lower()

    def test_resolve_action_signature_carries_no_llm_verdict_parameter(self):
        sig = inspect.signature(precedence.resolve_action)
        for name in sig.parameters:
            assert "llm" not in name.lower()

    def test_state_transition_function_takes_only_lifecycle_states(self):
        sig = inspect.signature(state.transition)
        for param in sig.parameters.values():
            annotation = str(param.annotation)
            assert "DevilsAdvocateReview" not in annotation
            assert "PortfolioDecision" not in annotation


class TestNoLlmDirectStateMutation:
    def test_position_lifecycle_state_transitions_are_validated_not_freely_assignable(self):
        """transition() is the only function in state.py that changes a
        state -- and it raises for any edge not in VALID_TRANSITIONS,
        so nothing (LLM-originated or otherwise) can force an illegal
        jump by simply calling a setter."""
        from src.lifecycle.state import InvalidLifecycleTransitionError, PositionLifecycleState as S

        with pytest.raises(InvalidLifecycleTransitionError):
            state.transition(S.CLOSED, S.ACTIVE)

    def test_lifecycle_position_record_is_frozen(self):
        from src.lifecycle.excursion import initial_excursion
        from src.lifecycle.persistence import LifecyclePositionRecord
        from src.lifecycle.state import PositionLifecycleState as S
        from src.strategies.base import StrategyKind

        rec = LifecyclePositionRecord(
            trade_id="T1", ticker="XYZ", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
            management_policy_name="X", current_state=S.ACTIVE, excursion=initial_excursion(0.0, datetime(2026, 1, 1, tzinfo=timezone.utc)),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with pytest.raises(Exception):
            rec.current_state = S.CLOSED  # type: ignore[misc]


class TestNoAutomaticFidelityExecution:
    def test_no_module_anywhere_in_lifecycle_package_imports_fidelity_credentials_module(self):
        """Defense in depth: none of src.lifecycle's own modules import
        anything Fidelity-session/credential-shaped -- the only
        Fidelity coupling is fidelity_events.py's own ticket-text
        rendering, which src.brokers.fidelity's own trusted-kernel
        audit (tests/acceptance/test_fidelity_manual_only.py) already
        separately covers end to end."""
        forbidden = re.compile(r"fidelity_session|fidelity_credentials|fidelity_login", re.IGNORECASE)
        for path in _lifecycle_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text)


class TestRollingAndAdjustmentNeverSelfApprove:
    def test_rolling_module_never_imports_risk_decision_approve_constant(self):
        """rolling.py builds bookkeeping only -- it must never itself
        construct or reference an APPROVE-shaped Risk decision, which
        would be the concrete signature of a self-approval path."""
        source = inspect.getsource(rolling)
        assert "RiskDecision.APPROVE" not in source

    def test_adjustment_module_never_imports_risk_decision_approve_constant(self):
        source = inspect.getsource(adjustment)
        assert "RiskDecision.APPROVE" not in source

    def test_neither_module_imports_risk_engine_at_all(self):
        """A roll/adjustment proposal is packaged here and evaluated by
        src.risk.engine elsewhere (by the caller) -- neither module
        needs or has its own IMPORT STATEMENT for src.risk.engine (a
        docstring mentioning it as documentation is fine and expected;
        an actual `import`/`from` line would mean this module could
        call it directly, which is what this check rules out)."""
        import_pattern = re.compile(r"^\s*(from|import)\s+src\.risk\.engine\b", re.MULTILINE)
        assert not import_pattern.search(inspect.getsource(rolling))
        assert not import_pattern.search(inspect.getsource(adjustment))
