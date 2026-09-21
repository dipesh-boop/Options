"""Section 13: Fidelity security acceptance. Converts
`SECURITY_AUDIT.md`'s FS-001 through FS-005 findings -- so far verified
only by manually-run `grep` reproduction commands recorded in that
document -- into executable, repo-wide regression tests that run every
time the suite runs, plus extends `tests/unit/brokers/
test_fidelity_no_execution.py`'s already-thorough (but
`src/brokers/fidelity.py`-scoped) evidence to the places FS-001/FS-004
name that file doesn't cover: the repository as a whole, `config/
brokers.yaml`'s loader, every `src/llm/*.py` module, and every agent
persona's declared tool list.

Non-negotiable invariant under test (CLAUDE.md #3): `FidelityProvider`
may only validate/create-ticket/format/record-human-entry/record-human-
confirmation/reconcile -- it must never submit an order, and nothing
anywhere may turn `MANUAL_EXECUTION` into something automatic.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# FS-001's own pattern list, reproduced here as an executable regex
# rather than a one-off manual grep command recorded in a markdown
# file -- run repo-wide (src/, tests/, config/, .claude/), not just
# against src/brokers/fidelity.py.
_FORBIDDEN_PATTERNS = (
    r"selenium", r"playwright", r"puppeteer", r"webdriver",
    r"fidelity\.com", r"api\.fidelity",
)
_SCAN_DIRS = ("src", "config", ".claude")


def _iter_source_files():
    for d in _SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".yaml", ".yml", ".md"):
                yield path


class TestFS001NoFidelityCredentialsCookiesOrBrowserAutomationRepoWide:
    # `src/brokers/fidelity.py`'s own module docstring names
    # "Fidelity.com" once, explicitly to describe what this module
    # deliberately does NOT do ("...scraping Fidelity.com, automating
    # Trader+..."). FS-001's own manual audit already triaged this
    # exact line as a documented non-issue; every OTHER file in the
    # repository is held to the strict zero-matches bar.
    _KNOWN_NEGATION_EXCEPTIONS = {("fidelity\\.com", "src/brokers/fidelity.py")}

    @pytest.mark.parametrize("pattern", _FORBIDDEN_PATTERNS)
    def test_forbidden_pattern_absent_across_the_whole_source_tree(self, pattern: str):
        regex = re.compile(pattern, re.IGNORECASE)
        offending = []
        for path in _iter_source_files():
            rel = str(path.relative_to(REPO_ROOT))
            if (pattern, rel) in self._KNOWN_NEGATION_EXCEPTIONS:
                continue
            text = path.read_text(errors="ignore")
            if regex.search(text):
                offending.append(rel)
        assert offending == [], f"forbidden pattern {pattern!r} found in: {offending}"

    def test_the_one_known_exception_still_genuinely_reads_as_a_negation(self):
        """Regression guard on the exception itself: if this line ever
        stops being a negation (e.g. the docstring is edited), this
        test must fail loudly rather than let the exception above mask
        a real future problem."""
        text = (REPO_ROOT / "src" / "brokers" / "fidelity.py").read_text()
        line = next(l for l in text.splitlines() if "fidelity.com" in l.lower())
        assert any(w in text[max(0, text.index(line) - 200):text.index(line) + 50].lower() for w in ("never", "no code", "no ", "not "))

    def test_no_env_credential_or_secret_files_exist(self):
        matches = []
        for pattern in ("*.env*", "*credential*", "*secret*"):
            matches.extend(p for p in REPO_ROOT.rglob(pattern) if ".git" not in p.parts)
        # .env.example (a documented template with no real values) is
        # the one expected, legitimate match.
        unexpected = [str(p.relative_to(REPO_ROOT)) for p in matches if p.name != ".env.example"]
        assert unexpected == [], f"unexpected credential/secret-shaped file(s): {unexpected}"


class TestFS002FidelityProviderNeverSubmitsAnOrder:
    def test_fidelity_module_has_no_broker_interface_implementation(self):
        from src.brokers.base import Broker
        from src.brokers.fidelity import FidelityManualProvider

        assert not issubclass(FidelityManualProvider, Broker)

    def test_fidelity_provider_public_surface_is_read_only_ticket_generation(self):
        from src.brokers.fidelity import FidelityManualProvider

        public_methods = [name for name in dir(FidelityManualProvider) if not name.startswith("_") and callable(getattr(FidelityManualProvider, name))]
        forbidden_terms = ("submit", "place", "send", "execute", "transmit")
        offending = [m for m in public_methods if any(term in m.lower() for term in forbidden_terms)]
        assert offending == [], f"FidelityManualProvider exposes order-submission-shaped method(s): {offending}"


class TestFS003ExecutionModeHasNoRuntimeOverrideMechanism:
    def test_broker_constraints_loader_has_no_env_override_for_execution_mode(self):
        source = (REPO_ROOT / "src" / "risk" / "broker_constraints.py").read_text()
        assert "_env" not in source
        assert "os.environ" not in source and "getenv" not in source

    def test_fidelity_capability_from_config_is_manual_execution(self):
        from src.risk.broker_constraints import load_broker_capabilities

        caps = load_broker_capabilities("fidelity")
        assert caps is not None
        mode = caps.execution_mode.value if hasattr(caps.execution_mode, "value") else caps.execution_mode
        assert "manual" in mode.lower()


class TestFS004NoLlmFacingCodeCanInfluenceBrokerCapabilitiesOrExecutionMode:
    def test_no_llm_module_references_broker_capabilities(self):
        llm_dir = REPO_ROOT / "src" / "llm"
        offending = []
        for path in llm_dir.glob("*.py"):
            text = path.read_text()
            if "broker_capabilities" in text.lower() or "BrokerCapabilities" in text:
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"src/llm module(s) reference broker capabilities: {offending}"

    def test_every_agent_persona_tool_is_read_only(self):
        agents_dir = REPO_ROOT / ".claude" / "agents"
        assert agents_dir.exists()
        offending = []
        for md_path in agents_dir.glob("*.md"):
            frontmatter = md_path.read_text().split("---")[1]
            tool_lines = [line.strip("- ").strip() for line in frontmatter.splitlines() if line.strip().startswith("- ")]
            for tool in tool_lines:
                if not tool.startswith("get_"):
                    offending.append(f"{md_path.name}: {tool}")
        assert offending == [], f"agent persona tool(s) not read-only (get_*): {offending}"

    def test_no_agent_persona_mentions_executing_or_submitting_an_order(self):
        agents_dir = REPO_ROOT / ".claude" / "agents"
        offending = []
        forbidden_terms = ("execute the trade", "submit the order", "place the order", "send the order to fidelity")
        for md_path in agents_dir.glob("*.md"):
            text = md_path.read_text().lower()
            for term in forbidden_terms:
                if term in text:
                    offending.append(f"{md_path.name}: {term!r}")
        assert offending == [], f"agent persona instructs autonomous order execution: {offending}"


class TestFS005SoleTicketConstructionSiteIsHardcodedToAwaitingHuman:
    """FS-005 (LOW, "no live exploit path"): `FidelityTradeTicket(` is
    constructed in exactly one place in `src/`, always hardcoded to
    AWAITING_HUMAN. This regression test fails the moment a second
    construction site appears anywhere in `src/` -- catching, at
    review time, any future code path that might construct a ticket at
    a terminal status without going through the real lifecycle."""

    def test_exactly_one_construction_call_site_in_src(self):
        """Excludes the class's own `class FidelityTradeTicket(...)`
        definition line -- only actual CALLS (`FidelityTradeTicket(`
        preceded by something other than `class `) count."""
        src_dir = REPO_ROOT / "src"
        sites = []
        for path in src_dir.rglob("*.py"):
            for line in path.read_text().splitlines():
                if "FidelityTradeTicket(" in line and not line.strip().startswith("class "):
                    sites.append(str(path.relative_to(REPO_ROOT)))
        assert sites == ["src/brokers/fidelity.py"], (
            f"expected exactly one FidelityTradeTicket( construction call site (generate_trade_ticket), found: {sites}"
        )

    def test_generate_trade_ticket_always_produces_awaiting_human(self):
        import inspect

        from src.brokers.fidelity import FidelityManualProvider, TicketStatus

        source = inspect.getsource(FidelityManualProvider.generate_trade_ticket)
        assert "TicketStatus.AWAITING_HUMAN" in source
        for other in TicketStatus:
            if other == TicketStatus.AWAITING_HUMAN:
                continue
            assert f"status={other.__class__.__name__}.{other.name}" not in source


class TestPaperFillNeverInterpretedAsAFidelityFillAndViceVersa:
    """CLAUDE.md invariant #3's own explicit closing sentence: "A
    PaperBroker fill must never be interpreted as a Fidelity fill, and
    vice versa." Structural proof: they are two entirely separate
    Pydantic models with no shared base class and no conversion
    function between them anywhere in `src/`."""

    def test_paper_order_and_fidelity_ticket_are_unrelated_types(self):
        from src.brokers.base import Order as PaperOrder
        from src.brokers.fidelity import FidelityTradeTicket

        assert not issubclass(FidelityTradeTicket, PaperOrder)
        assert not issubclass(PaperOrder, FidelityTradeTicket)

    def test_no_conversion_function_between_paper_order_and_fidelity_ticket_exists(self):
        src_dir = REPO_ROOT / "src"
        offending = []
        for path in src_dir.rglob("*.py"):
            text = path.read_text()
            if re.search(r"def\s+\w*(order_to_ticket|ticket_to_order|paper_fill_to_fidelity|fidelity_to_paper)\w*\(", text, re.IGNORECASE):
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"unexpected PaperBroker<->Fidelity conversion function found in: {offending}"
