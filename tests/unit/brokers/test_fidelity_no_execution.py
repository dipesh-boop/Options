"""Proof that FidelityManualProvider cannot submit an automated stock
or options order — the central requirement of this module.

Four independent lines of evidence:
1. Static: the module imports nothing capable of a network call, browser
   automation, or shelling out.
2. Static: no class in the module has any method whose name suggests
   order submission, and it does not implement `Broker`.
3. Static: no class in the module has a field that could hold a
   Fidelity credential, session cookie, or MFA bypass.
4. Runtime (the strongest proof): generating a ticket, end to end,
   never opens a network socket — proven by patching `socket.socket` to
   raise if constructed at all during the call.
"""
from __future__ import annotations

import inspect
import re
import socket
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.brokers import fidelity
from src.brokers.base import Broker
from src.brokers.fidelity import (
    ApprovedOrder,
    ExecutionMode,
    FidelityLegAction,
    FidelityManualProvider,
    FidelityOrderLeg,
)
from src.data.option_chain import OptionRight

FIDELITY_MODULE_PATH = Path(fidelity.__file__)
FIDELITY_SOURCE = FIDELITY_MODULE_PATH.read_text(encoding="utf-8")


def _valid_approved_order(**overrides) -> ApprovedOrder:
    now = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
    md = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
    base = dict(
        risk_approval_id="risk-approval-123",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.50,
        expiration=date(2026, 10, 16),
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=date(2026, 10, 16), contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=date(2026, 10, 16), contracts=2),
        ],
        quantity=2,
        limit_price=1.35,
        estimated_credit_debit=1.35,
        max_profit=270.0,
        max_loss=730.0,
        breakeven=618.65,
        return_on_capital=270 / 730,
        profit_target=0.68,
        loss_management_rule="Close or roll if loss reaches 2x credit received.",
        DTE_management_rule="Review/close/roll according to strategy rules at 21 DTE.",
        timestamp=now,
        market_data_timestamp=md,
    )
    base.update(overrides)
    return ApprovedOrder(**base)


# ---------------------------------------------------------------- 1 ----


class TestNoNetworkOrAutomationImports:
    FORBIDDEN_IMPORT_PATTERNS = [
        r"^\s*import\s+requests\b",
        r"^\s*from\s+requests\b",
        r"^\s*import\s+httpx\b",
        r"^\s*from\s+httpx\b",
        r"^\s*import\s+aiohttp\b",
        r"^\s*from\s+aiohttp\b",
        r"^\s*import\s+urllib\b",
        r"^\s*from\s+urllib\b",
        r"^\s*import\s+socket\b",
        r"^\s*from\s+socket\b",
        r"^\s*import\s+selenium\b",
        r"^\s*from\s+selenium\b",
        r"^\s*import\s+playwright\b",
        r"^\s*from\s+playwright\b",
        r"^\s*import\s+pyppeteer\b",
        r"^\s*from\s+pyppeteer\b",
        r"^\s*import\s+webdriver\b",
        r"^\s*import\s+subprocess\b",
        r"^\s*from\s+subprocess\b",
        r"^\s*import\s+ib_insync\b",  # this is a Fidelity provider, not IBKR
    ]

    @pytest.mark.parametrize("pattern", FORBIDDEN_IMPORT_PATTERNS)
    def test_forbidden_import_absent(self, pattern: str):
        assert re.search(pattern, FIDELITY_SOURCE, re.MULTILINE) is None, f"forbidden import matched {pattern!r}"

    def test_no_os_system_or_exec_calls(self):
        assert "os.system(" not in FIDELITY_SOURCE
        assert "os.popen(" not in FIDELITY_SOURCE
        assert re.search(r"\beval\s*\(", FIDELITY_SOURCE) is None
        assert re.search(r"\bexec\s*\(", FIDELITY_SOURCE) is None

    def test_no_src_llm_import(self):
        assert re.search(r"^\s*from\s+src\.llm\b", FIDELITY_SOURCE, re.MULTILINE) is None
        assert re.search(r"^\s*import\s+src\.llm\b", FIDELITY_SOURCE, re.MULTILINE) is None


# ---------------------------------------------------------------- 2 ----


class TestNoOrderSubmissionCapability:
    FORBIDDEN_METHOD_NAME_SUBSTRINGS = ["place_order", "submit_order", "send_order", "execute_order", "place_trade", "submit_trade"]

    def test_provider_does_not_implement_broker(self):
        assert not issubclass(FidelityManualProvider, Broker)

    def test_no_method_name_suggests_order_submission(self):
        method_names = [name for name, _ in inspect.getmembers(FidelityManualProvider, predicate=inspect.isfunction)]
        for name in method_names:
            for forbidden in self.FORBIDDEN_METHOD_NAME_SUBSTRINGS:
                assert forbidden not in name.lower(), f"method {name!r} looks order-submission-capable"

    def test_provider_has_exactly_one_capability_method(self):
        # generate_trade_ticket is the only thing this class does.
        own_methods = [
            name
            for name, _ in inspect.getmembers(FidelityManualProvider, predicate=inspect.isfunction)
            if not name.startswith("_")
        ]
        assert own_methods == ["generate_trade_ticket"]

    def test_execution_mode_is_manual_only(self):
        assert FidelityManualProvider.execution_mode == ExecutionMode.MANUAL_EXECUTION
        assert list(ExecutionMode) == [ExecutionMode.MANUAL_EXECUTION]

    def test_generate_trade_ticket_return_type_is_never_a_confirmation(self):
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_valid_approved_order())
        from src.brokers.fidelity import TicketStatus

        assert ticket.status == TicketStatus.AWAITING_HUMAN
        assert ticket.execution_confirmation is None


# ---------------------------------------------------------------- 3 ----


class TestNoCredentialOrSessionStorage:
    FORBIDDEN_FIELD_SUBSTRINGS = [
        "password",
        "username",
        "cookie",
        "session_token",
        "session_id",
        "auth_token",
        "api_key",
        "apikey",
        "mfa_bypass",
        "otp",
        "secret",
    ]

    def _all_model_field_names(self):
        from pydantic import BaseModel

        names: set[str] = set()
        for _, obj in inspect.getmembers(fidelity):
            if inspect.isclass(obj) and issubclass(obj, BaseModel):
                names.update(obj.model_fields.keys())
        return names

    def test_no_model_has_a_credential_shaped_field(self):
        field_names = self._all_model_field_names()
        for field_name in field_names:
            lowered = field_name.lower()
            for forbidden in self.FORBIDDEN_FIELD_SUBSTRINGS:
                assert forbidden not in lowered, f"field {field_name!r} looks credential-shaped"

    def test_no_forbidden_substring_anywhere_in_source_as_an_identifier(self):
        # Broader net: forbidden substrings shouldn't appear as
        # identifiers (assignment targets, parameter names) anywhere in
        # the module, not just as Pydantic fields.
        identifier_pattern = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]")
        identifiers = {m.group(1).lower() for m in identifier_pattern.finditer(FIDELITY_SOURCE)}
        for forbidden in ("password", "username", "cookie", "mfa_bypass"):
            assert forbidden not in identifiers


# ---------------------------------------------------------------- 4 ----


class TestNoNetworkActivityAtRuntime:
    def test_generate_trade_ticket_never_opens_a_socket(self, monkeypatch: pytest.MonkeyPatch):
        def _raise_if_socket_constructed(*args, **kwargs):
            raise AssertionError("fidelity.py attempted to open a network socket")

        monkeypatch.setattr(socket, "socket", _raise_if_socket_constructed)

        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_valid_approved_order())
        assert ticket is not None  # completed without ever touching the patched constructor

    def test_render_ticket_text_never_opens_a_socket(self, monkeypatch: pytest.MonkeyPatch):
        from src.brokers.fidelity import render_ticket_text

        def _raise_if_socket_constructed(*args, **kwargs):
            raise AssertionError("render_ticket_text attempted to open a network socket")

        monkeypatch.setattr(socket, "socket", _raise_if_socket_constructed)

        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_valid_approved_order())
        text = render_ticket_text(ticket)
        assert "FIDELITY TRADE TICKET" in text

    def test_full_lifecycle_never_opens_a_socket(self, monkeypatch: pytest.MonkeyPatch):
        from src.brokers.fidelity import ExecutionConfirmation, confirm_fill, transition
        from src.brokers.fidelity import TicketStatus

        def _raise_if_socket_constructed(*args, **kwargs):
            raise AssertionError("ticket lifecycle attempted to open a network socket")

        monkeypatch.setattr(socket, "socket", _raise_if_socket_constructed)

        now = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
        provider = FidelityManualProvider()
        ticket = provider.generate_trade_ticket(_valid_approved_order())
        ticket = transition(ticket, TicketStatus.ORDER_ENTERED, at=now)
        confirmation = ExecutionConfirmation(
            confirmed_by="human:test", confirmation_source="human_manual_entry", filled_quantity=2, fill_price=1.35, confirmed_at=now
        )
        ticket = confirm_fill(ticket, confirmation)
        assert ticket.status == TicketStatus.FILLED
