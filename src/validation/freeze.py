"""Step 22 Parts 20-24: the PAPER_TRADING_V1.0 freeze manifest.

This module builds and verifies `VALIDATION_MANIFEST.json` -- the single
artifact that answers "what exact code, config, and assumptions was this
90-day validation run actually measuring." It is deliberately built on
top of (not a replacement for) `src.validation.protocol`'s existing
`StrategyVersionManifest`/`verify_manifest_integrity`/`ManifestDriftError`
machinery, which already does file-hash-based drift detection for the
three strategy-defining YAML files; this module widens that same
fail-loud-on-drift idea to *every* file/module Part 21 names as part of
the frozen experiment (prompts, agent personas, the Quant/Risk/PaperBroker
source itself, the market calendar), and adds the handful of fields that
aren't file hashes at all (git commit, Python version, dependency
versions, fill-model/slippage/commission assumptions, and the three
explicit safety flags a reader must never have to infer:
`live_trading_enabled`, `automatic_fidelity_execution`,
`validation_cohort_started` -- all `False` at freeze time, always).

**Material vs non-material change (Part 23):** every file/module hashed
into this manifest is, by definition, material -- that is the whole
point of listing it here rather than leaving it out. A change to any of
them after the freeze is exactly the "material configuration drift" Part
22 describes, and `verify_freeze` reports it as a hash mismatch, never
silently. Genuinely non-material changes (a typo in a comment inside a
module that isn't hashed as a whole-file digest of *behavior*... but see
the caveat below) are, deliberately, changes to files this manifest does
NOT hash at all -- e.g. README.md, ARCHITECTURE.md's prose, this
module's own docstring wording. Because the code-module hashes below are
whole-file SHA-256 digests, a purely cosmetic change inside an hashed
module (renaming a local variable, rewording a comment) *would* register
as drift even though it changes no behavior -- this is a deliberate,
documented over-approximation (fail loud on possible drift rather than
try to distinguish "cosmetic" from "behavioral" automatically, which
would require semantic diffing this codebase does not attempt), not a
bug. A reported drift always deserves a human look, even if that look
concludes it was cosmetic -- it must never be silently reclassified as
non-material by the tooling itself.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.reporting.data import EXPORT_SCHEMA_VERSION
from src.validation.protocol import DEFAULT_CONFIG_PATH, compute_file_hash, load_validation_config
from src.validation.session import DATABASE_SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]

# Step 22.1 (Alpaca market-data amendment), Step 22.2 (stateful Wheel
# strategy amendment), Step 22.3 (Strategy Lifecycle Management Engine
# amendment), Step 22.4 (Tradier market data + Portfolio Control Loop
# amendment), Step 22.4A (outer orchestrator + dashboard projection
# acceptance remediation), Step 22.4B (test-portability/fixture
# remediation only), Step 22.4C (SQLite backup acceptance-test
# semantic-verification remediation only -- no production behavior
# changed), Step 22.5 (PAPER_TRADING_V1.4.4: the Review-Only
# operational runtime -- production module hashes for src/brokers/,
# src/portfolio/, and the new src/review/ package legitimately changed
# that step), Step 22.6 (PAPER_TRADING_V1.4.5: operator CLI/
# preflight safety remediation ONLY -- scripts/run_validation_cycle.py
# gained real CLI argument parsing (--help/--preflight never mutate
# validation state) and a Tradier-production-provider preflight check
# before any mutation; src/data/factory.py gained the policy function
# that check calls. Does NOT modify frozen strategy, Quant,
# deterministic Risk, lifecycle policy, or trade-selection behavior --
# see STEP_22_6_FREEZE_REPORT.md), and Step 22.7 (PAPER_TRADING_V1.4.6:
# Tradier market-data timestamp semantics / freshness safety hotfix
# ONLY -- src/data/tradier_provider.py's canonical timestamp selection
# now prefers the freshest valid bid/ask quote timestamp over a
# possibly-stale last-trade timestamp, and src/data/provider.py's
# shared TimestampedModel gained a defensive rule so a provider
# timestamp materially in the future can never read as FRESH. Does NOT
# modify frozen strategy, Quant, deterministic Risk, lifecycle policy,
# or trade-selection behavior, and does NOT loosen
# DEFAULT_MAX_QUOTE_AGE or any other freshness threshold -- see
# STEP_22_7_FREEZE_REPORT.md), and Step 22.8 (PAPER_TRADING_V1.4.7:
# operator usability / daily-startup hotfix ONLY -- fixed a bug where
# the shipped .env template's intentionally-blank optional values
# (sourced via `set -a; source .env; set +a`) crashed config parsing
# (int('')/float('')); bumped stale PAPER_TRADING_V1.4.5 operator-
# facing version labels to V1.4.7; added a read-only dashboard
# operator-status view and the one new dashboard action that may run
# the daily validation cycle (reusing scripts/run_validation_cycle.py's
# own unmodified, already-safety-proven `run_validation_cycle()`
# coroutine in-process -- never a reimplementation); added a portable,
# repo-relative macOS one-click launcher that starts the dashboard
# only, never the validation cycle, never a candidate confirmation.
# Does NOT modify frozen strategy, Quant, deterministic Risk, lifecycle
# policy, PaperBroker fill model, Fidelity behavior, or trade-selection
# behavior, and does NOT touch the active validation cohort/database in
# any way -- see STEP_22_8_FREEZE_REPORT.md), and Step 22.9
# (PAPER_TRADING_V1.4.8: operator dashboard UI completion ONLY -- wired
# the existing V1.4.7 backend operator APIs
# (GET /api/operator-status, POST /api/validation-cycle/run) into the
# actual rendered dashboard for the first time via a new,
# DOM-free pure-logic module (src/dashboard/static/operator_control.js)
# plus additive read-only fields on OperatorStatusView (cohort start/
# planned-end date, preferred completed-trade target, unresolved
# alerts); the Run Daily Validation button only ever calls the
# existing, unmodified POST endpoint, is fail-closed (disabled unless
# configured, not-yet-run-today, and provider-ready), and is guarded
# against double-submission; candidate confirmation stays CLI-only --
# no dashboard control of any kind was added for it. Does NOT modify
# frozen strategy, Quant, deterministic Risk, lifecycle policy,
# PaperBroker fill model, Fidelity behavior, or trade-selection
# behavior, adds no new dashboard route, and does NOT touch the active
# validation cohort/database in any way -- see
# STEP_22_9_FREEZE_REPORT.md) each bumped the freeze
# name/version in place without touching the prior versions' own
# artifacts -- see progress.md and STEP_22_1_FREEZE_REPORT.md /
# STEP_22_2_FREEZE_REPORT.md / STEP_22_3_FREEZE_REPORT.md /
# STEP_22_4_FREEZE_REPORT.md / STEP_22_4A_FREEZE_REPORT.md /
# STEP_22_4B_FREEZE_REPORT.md / STEP_22_4C_FREEZE_REPORT.md /
# STEP_22_5_FREEZE_REPORT.md / STEP_22_6_FREEZE_REPORT.md /
# STEP_22_7_FREEZE_REPORT.md / STEP_22_8_FREEZE_REPORT.md /
# STEP_22_9_FREEZE_REPORT.md.
# FREEZE_NAME/MANIFEST_VERSION always
# reflect the *current* frozen state; the original V1.0/V1.1/V1.2/V1.3/
# V1.4/V1.4.1/V1.4.2/V1.4.3/V1.4.4/V1.4.5/V1.4.6/V1.4.7 manifests/reports
# remain recoverable from git history at the `paper-trading-v1.0` /
# `paper-trading-v1.1` / `paper-trading-v1.2` / `paper-trading-v1.3` /
# `paper-trading-v1.4` / `paper-trading-v1.4.1` / `paper-trading-v1.4.2`
# / `paper-trading-v1.4.3` / `paper-trading-v1.4.4` / `paper-trading-v1.4.5`
# / `paper-trading-v1.4.6` / `paper-trading-v1.4.7` tags.
FREEZE_NAME = "PAPER_TRADING_V1.4.8"
MANIFEST_FILENAME = "VALIDATION_MANIFEST.json"
MANIFEST_VERSION = "1.4.8"

_CONFIG_DIR = REPO_ROOT / "config"
_AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# The config files that actually exist in this repository as of Step 22.
# Part 21 also names `strategies.yaml` -- this platform never split that
# out into its own file (per-strategy behavior lives in
# `src/strategies/*.py`, hashed below as code), so that key is recorded
# as an explicit "not applicable" entry rather than silently omitted.
# `universe.yaml` WAS "not applicable" (the tradable universe was not
# yet config-driven at all) through V1.4.3 -- Step 22.5 (PAPER_TRADING_V1.4.4)
# adds it, alongside the new `operations.yaml`, as real, hashed config.
_NAMED_CONFIG_FILES: dict[str, Path | None] = {
    "risk_limits.yaml": _CONFIG_DIR / "risk_limits.yaml",
    "brokers.yaml": _CONFIG_DIR / "brokers.yaml",
    "validation.yaml": _CONFIG_DIR / "validation.yaml",
    "llm.yaml": _CONFIG_DIR / "llm.yaml",
    "strategies.yaml": None,  # not applicable -- see module docstring
    # Step 22.5 (PAPER_TRADING_V1.4.4): the frozen-universe ticker/strategy
    # list and the operational-runtime configuration for the daily
    # validation-cycle runner and confirm-candidate command.
    "universe.yaml": _CONFIG_DIR / "universe.yaml",
    "operations.yaml": _CONFIG_DIR / "operations.yaml",
}

# Whole-module (directory) hashes for the code that actually defines
# strategy/risk/execution behavior -- the file-hash-based drift
# detection `src.validation.protocol` already applies to the 3 YAML
# files, widened to the source itself.
_CODE_MODULE_DIRS: dict[str, Path] = {
    "quant_module": REPO_ROOT / "src" / "quant",
    "risk_module": REPO_ROOT / "src" / "risk",
    "strategies_module": REPO_ROOT / "src" / "strategies",
    # Step 22.2: the stateful Wheel package -- hashing it means any
    # future change (including one that tried to weaken the
    # UncoveredCallError check or bypass the Risk Engine) is caught as
    # material drift.
    "wheel_module": REPO_ROOT / "src" / "wheel",
    # Step 22.3: the Strategy Lifecycle Management Engine -- hashing it
    # means any future change (including one that tried to widen
    # RISK_EXIT_REQUIRED's escape hatches, weaken a trigger, or let a
    # roll/adjustment self-approve) is caught as material drift.
    "lifecycle_module": REPO_ROOT / "src" / "lifecycle",
    # Step 22.4: the Portfolio Control Loop -- hashing it means any
    # future change (including one that tried to give the control loop
    # order-placement capability, bypass the Risk Engine, or skip
    # calling the unmodified Lifecycle Engine) is caught as material
    # drift.
    "portfolio_module": REPO_ROOT / "src" / "portfolio",
    # Step 22.5: `src.review.confirmation.confirm_candidate` is the ONLY
    # function anywhere in this codebase that may call
    # `PaperBroker.place_order` for a new position during this cohort's
    # validation -- hashing this whole package means any future change
    # (including one that tried to let the unattended daily scan fill a
    # position, weaken the mandatory revalidation sequence, or fake an
    # LLM review) is caught as material drift.
    "review_module": REPO_ROOT / "src" / "review",
}
_CODE_MODULE_FILES: dict[str, Path] = {
    "paper_broker_module": REPO_ROOT / "src" / "brokers" / "paper.py",
    "market_calendar_module": REPO_ROOT / "src" / "data" / "market_calendar.py",
    # Step 22.1: Alpaca is market-data-only (never execution) -- hashing
    # this file means any future change to it (including one that tried
    # to add order-submission capability) is caught as material drift.
    "alpaca_provider_module": REPO_ROOT / "src" / "data" / "alpaca_provider.py",
    # Step 22.4: Tradier is market-data-only (never execution) -- same
    # reasoning as Alpaca above, applied to the second real provider.
    "tradier_provider_module": REPO_ROOT / "src" / "data" / "tradier_provider.py",
    "rate_limiter_module": REPO_ROOT / "src" / "data" / "rate_limiter.py",
    "quality_gate_module": REPO_ROOT / "src" / "data" / "quality_gate.py",
    # Step 22.4A: the operator-run Tradier production smoke test --
    # hashing it means any future change (including one that tried to
    # add an order/trading call or print the raw token) is caught as
    # material drift, exactly like the provider module it exercises.
    "smoke_tradier_script": REPO_ROOT / "scripts" / "smoke_tradier_market_data.py",
    # Step 22.4A: the dashboard's control-loop projection/loading
    # mechanism -- the one place persisted control-loop output is read
    # into `DashboardState`. `src/portfolio/orchestrator.py` (the new
    # outer orchestrator) needs no separate entry here: it lives inside
    # `src/portfolio/`, already covered by `portfolio_module_hash`'s
    # whole-directory hash below.
    "control_loop_projection_module": REPO_ROOT / "src" / "dashboard" / "control_loop_projection.py",
    # Step 22.5: the two new operator entry points -- the unattended
    # daily cycle runner (which must never call `PaperBroker.place_order`)
    # and the human confirm-candidate command (the only thing that may).
    # Hashing them means any future change to either is caught as
    # material drift, exactly like `smoke_tradier_script` above.
    "run_validation_cycle_script": REPO_ROOT / "scripts" / "run_validation_cycle.py",
    "confirm_candidate_script": REPO_ROOT / "scripts" / "confirm_candidate.py",
    # Step 22.6: `verify_official_provider_is_tradier_production` -- the
    # policy check that decides whether the official, state-mutating
    # validation cycle is even allowed to proceed -- lives here. Hashing
    # this file means any future change (including one that quietly
    # widened the approved provider set, dropped the token check, or
    # accepted Tradier's sandbox host) is caught as material drift.
    "factory_module": REPO_ROOT / "src" / "data" / "factory.py",
    # Step 22.7: `TimestampedModel` -- the shared canonical freshness
    # primitive (`age`/`freshness_status`/`require_fresh`) every
    # `UnderlyingQuote`/`OptionContract`/`OptionChain` inherits, and the
    # one place the future-timestamp defensive rule lives. Hashing this
    # file means any future change (including one that quietly widened
    # `DEFAULT_MAX_QUOTE_AGE`, removed the future-timestamp guard, or
    # let `age > max_age` be bypassed) is caught as material drift --
    # not previously hashed anywhere, so newly added here.
    "data_provider_module": REPO_ROOT / "src" / "data" / "provider.py",
    # Step 22.8: the dashboard app gained two new routes --
    # GET /api/operator-status and POST /api/validation-cycle/run, the
    # latter the one new dashboard action capable of running the daily
    # cycle at all. `src/dashboard/app.py` itself was a pre-existing,
    # documented coverage gap (no prior step hashed the whole-file
    # dashboard app despite hashing e.g. `control_loop_projection.py`)
    # -- closed here alongside the brand-new `validation_ops.py`, the
    # one file that may call `run_validation_cycle()` in-process.
    # Hashing both means any future change (including one that widened
    # the trigger route to accept a candidate id, or added a route
    # shaped like AUTO TRADE/EXECUTE) is caught as material drift.
    "dashboard_app_module": REPO_ROOT / "src" / "dashboard" / "app.py",
    "dashboard_validation_ops_module": REPO_ROOT / "src" / "dashboard" / "validation_ops.py",
}

# For the formal 90-day validation, OPRA is the required options feed
# (Part 5) -- recorded here as policy, not as a runtime-enforced value:
# an operator may still run locally against `indicative`/`mock`/`ibkr`
# for development, but a freeze verification against this manifest
# records what the *validation* itself requires.
REQUIRED_OPTIONS_FEED_FOR_VALIDATION = "opra"


class FreezeManifestError(RuntimeError):
    """Raised when building or loading a freeze manifest fails outright
    (missing file, unreadable git repo, malformed JSON) -- fails closed,
    the same idiom every other config loader in this codebase uses."""


class FreezeCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str


class FreezeVerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    checks: tuple[FreezeCheck, ...]

    @property
    def failures(self) -> tuple[FreezeCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)


class FreezeManifest(BaseModel):
    """The full Part 21 field list. Every field is either a direct read
    of an existing config/source value or an explicit hash -- nothing
    here is invented or estimated."""

    model_config = ConfigDict(frozen=True)

    manifest_version: str
    freeze_name: str
    freeze_timestamp: datetime

    git_commit: str
    git_branch: str
    repository_state: str  # "clean" or "dirty (N uncommitted change(s))"

    python_version: str
    dependency_requirements_hash: str

    strategy_library_version: str  # hash of src/strategies/
    approved_strategies_by_broker: dict[str, tuple[str, ...]]

    config_file_hashes: dict[str, str | None]  # None where not applicable (see docstring)
    claude_md_hash: str
    prompt_file_hashes: dict[str, str]  # .claude/agents/*.md

    quant_module_hash: str
    risk_module_hash: str
    paper_broker_module_hash: str
    market_calendar_module_hash: str
    market_data_provider_config_version: str  # no separate config file today -- see note field
    market_data_provider_config_note: str

    # Step 22.1 (Alpaca market-data amendment).
    freeze_version: str
    alpaca_provider_module_hash: str
    data_provider_at_freeze_time: str  # OPTIONS_AGENT_DATA_PROVIDER as configured when frozen (operator-changeable afterward)
    required_options_feed_for_validation: str  # policy: OPRA required for the formal 90-day run

    # Step 22.2 (stateful Wheel strategy amendment).
    wheel_module_hash: str
    wheel_strategy_kind_trade_proposal_eligible: bool  # must always be False -- WHEEL never becomes its own order type

    # Step 22.3 (Strategy Lifecycle Management Engine amendment).
    lifecycle_module_hash: str
    lifecycle_named_policy_count: int  # >= 19 at freeze time -- see src.lifecycle.policies_library

    # Step 22.4 (Tradier market data + Portfolio Control Loop amendment).
    tradier_provider_module_hash: str
    rate_limiter_module_hash: str
    quality_gate_module_hash: str
    portfolio_module_hash: str
    tradier_market_data_only: bool  # must always be True -- Tradier has no order-submission code path
    control_loop_cannot_execute_trades: bool  # must always be True -- src/portfolio/ places no order, ever

    # Step 22.4A (outer orchestrator + dashboard projection amendment).
    smoke_tradier_script_hash: str
    control_loop_projection_module_hash: str
    dashboard_cannot_execute_trades: bool  # must always be True -- src/dashboard/ places no order, ever
    orchestrator_cannot_bypass_risk_or_lifecycle: bool  # must always be True -- no direct src.risk.engine/src.lifecycle.engine import, no confirm_fill call
    opportunity_scan_never_outranks_risk_monitoring: bool  # must always be True -- P4 opportunity scan priority is strictly lower than P3/P0 risk monitoring priorities

    # Step 22.5 (PAPER_TRADING_V1.4.4, Review-Only operational runtime).
    review_module_hash: str
    run_validation_cycle_script_hash: str
    confirm_candidate_script_hash: str
    daily_cycle_never_calls_place_order: bool  # must always be True -- scripts/run_validation_cycle.py never calls PaperBroker.place_order
    review_only_path_never_imports_llm: bool  # must always be True -- no real or faked LLM review anywhere on the new-position confirmation path

    # Step 22.6 (PAPER_TRADING_V1.4.5): operator CLI/preflight safety
    # remediation only -- does not modify frozen strategy, Quant,
    # deterministic Risk, lifecycle policy, or trade-selection behavior.
    factory_module_hash: str
    help_cannot_execute_validation: bool  # must always be True -- scripts/run_validation_cycle.py parses CLI args (argparse) before any mutating call
    official_cycle_requires_tradier_preflight: bool  # must always be True -- the mutating cycle calls verify_official_provider_is_tradier_production before its first mutation

    # Step 22.7 (PAPER_TRADING_V1.4.6): Tradier market-data timestamp
    # semantics / freshness safety hotfix only -- does not modify
    # frozen strategy, Quant, deterministic Risk, lifecycle policy, or
    # trade-selection behavior. `data_provider_module_hash` is a
    # previously-uncovered file (`src/data/provider.py`, the shared
    # `TimestampedModel` freshness primitive every canonical quote/
    # contract/chain in this codebase inherits) newly hashed because
    # this step is the first to change it.
    data_provider_module_hash: str

    # Step 22.8 (PAPER_TRADING_V1.4.7): operator usability/daily-startup
    # hotfix only -- .env blank-optional-value handling, operator-
    # facing version labels, a read-only dashboard operator-status
    # view, the one new dashboard action that may run the daily
    # validation cycle (reusing that script's own unmodified,
    # already-safety-proven code path), and a macOS one-click launcher.
    # Does NOT modify frozen strategy, Quant, deterministic Risk,
    # lifecycle policy, PaperBroker fill model, Fidelity behavior, or
    # trade-selection behavior, and does NOT change the active
    # validation cohort/database in any way.
    dashboard_app_module_hash: str
    dashboard_validation_ops_module_hash: str
    dashboard_cannot_confirm_candidates: bool  # must always be True -- no src.dashboard file imports src.review.confirmation or confirm_candidate

    fill_model_assumptions: dict[str, Any]
    slippage_assumptions: dict[str, Any]
    commission_assumptions: dict[str, Any]
    option_multiplier_assumption: int

    benchmark_definitions: tuple[str, ...]

    starting_nav_default: float
    validation_duration_days: int
    minimum_sample_size: int
    preferred_sample_size: int
    drawdown_thresholds: dict[str, float]
    research_targets: dict[str, float]

    database_schema_version: str
    report_export_schema_version: str

    fidelity_execution_mode: str
    live_trading_enabled: bool
    automatic_fidelity_execution: bool
    validation_cohort_started: bool

    manifest_hash: str


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise FreezeManifestError(f"git {' '.join(args)} failed: {exc}") from exc
    return result.stdout.strip()


def _repository_state() -> str:
    status = _run_git("status", "--porcelain")
    if not status:
        return "clean"
    n = len([line for line in status.splitlines() if line.strip()])
    return f"dirty ({n} uncommitted change(s))"


def _hash_directory(path: Path) -> str:
    """Sorted, path-qualified concatenation of every `*.py` file's own
    SHA-256 hash, itself re-hashed -- deterministic regardless of
    filesystem iteration order, and sensitive to added/removed/renamed
    files, not just edited ones."""
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*.py")):
        if "__pycache__" in file_path.parts:
            continue
        rel = file_path.relative_to(REPO_ROOT).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(compute_file_hash(file_path).encode("utf-8"))
    return digest.hexdigest()


def _hash_files_in_dir(path: Path, pattern: str) -> dict[str, str]:
    return {
        f.name: compute_file_hash(f)
        for f in sorted(path.glob(pattern))
        if f.is_file()
    }


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, default=str)


def compute_manifest_hash(manifest_data: dict[str, Any]) -> str:
    """Hashes every field except `manifest_hash` itself. `manifest_data`
    must already be in the *JSON-serialized* shape (e.g. `datetime` as
    an ISO-8601 string, not a raw `datetime` object) -- i.e. the output
    of `json.loads(some_manifest.model_dump_json())`, exactly as
    `verify_freeze` produces it when re-hashing a loaded manifest. This
    keeps hashing symmetric between build time and verify time; see
    `_build_time_manifest_hash` for the build-time caller, which routes
    through the identical pydantic JSON round-trip before hashing so a
    freshly built manifest and a reloaded one always hash identically."""
    payload = {k: v for k, v in manifest_data.items() if k != "manifest_hash"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _build_time_manifest_hash(manifest_data_without_hash: dict[str, Any]) -> str:
    """Computes the hash a freshly built manifest should carry, by
    constructing a throwaway `FreezeManifest` (so `datetime`/tuple
    fields go through the exact same JSON serialization pydantic will
    later use when this manifest is saved and reloaded) and hashing
    that JSON-shaped payload with `compute_manifest_hash` -- the same
    function `verify_freeze` calls on a *loaded* manifest. Building and
    verifying therefore always agree, instead of one hashing raw Python
    objects and the other hashing their JSON-round-tripped form."""
    temp = FreezeManifest(**manifest_data_without_hash, manifest_hash="pending")
    return compute_manifest_hash(json.loads(temp.model_dump_json()))


def _current_data_provider_selection() -> str:
    from src.data.factory import DataProviderSelection

    return DataProviderSelection().data_provider


def _wheel_is_trade_proposal_eligible() -> bool:
    from src.strategies.base import TRADE_PROPOSAL_ELIGIBLE, StrategyKind

    return StrategyKind.WHEEL in TRADE_PROPOSAL_ELIGIBLE


def _lifecycle_named_policy_count() -> int:
    from src.lifecycle.policies_library import POLICIES_LIBRARY

    return len(POLICIES_LIBRARY)


def build_freeze_manifest(*, generated_at: datetime | None = None) -> FreezeManifest:
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise FreezeManifestError("generated_at must be timezone-aware")

    brokers_cfg = _load_yaml(_CONFIG_DIR / "brokers.yaml")
    fidelity_cfg = brokers_cfg.get("fidelity", {})
    fidelity_execution_mode = str(fidelity_cfg.get("execution_mode", ""))
    if fidelity_execution_mode != "MANUAL":
        # Fail loud rather than freeze a manifest that would misrepresent
        # CLAUDE.md invariant #3 -- this should be structurally
        # impossible given config/brokers.yaml's own committed content,
        # but the freeze process itself must never assume it.
        raise FreezeManifestError(
            f"refusing to build a freeze manifest: config/brokers.yaml fidelity.execution_mode "
            f"is {fidelity_execution_mode!r}, not 'MANUAL' -- CLAUDE.md invariant #3 requires "
            "Fidelity execution to remain manual-only at all times"
        )

    approved_strategies_by_broker = {
        broker: tuple(cfg.get("allowed_strategies", [])) for broker, cfg in brokers_cfg.items()
    }

    config_file_hashes: dict[str, str | None] = {
        name: (compute_file_hash(path) if path is not None else None)
        for name, path in _NAMED_CONFIG_FILES.items()
    }

    validation_config = load_validation_config()
    risk_limits_raw = _load_yaml(_CONFIG_DIR / "risk_limits.yaml")
    drawdown = risk_limits_raw.get("drawdown", {})

    requirements_path = REPO_ROOT / "requirements.txt"

    from src.brokers.paper import PaperBrokerConfig
    from src.backtest.commissions import CommissionSchedule

    default_paper_cfg = PaperBrokerConfig()
    default_commission = CommissionSchedule()

    manifest_data: dict[str, Any] = dict(
        manifest_version=MANIFEST_VERSION,
        freeze_name=FREEZE_NAME,
        freeze_timestamp=generated_at,
        git_commit=_run_git("rev-parse", "HEAD"),
        git_branch=_run_git("rev-parse", "--abbrev-ref", "HEAD"),
        repository_state=_repository_state(),
        python_version=sys.version.split()[0] + f" ({platform.python_implementation()})",
        dependency_requirements_hash=compute_file_hash(requirements_path),
        strategy_library_version=_hash_directory(_CODE_MODULE_DIRS["strategies_module"]),
        approved_strategies_by_broker=approved_strategies_by_broker,
        config_file_hashes=config_file_hashes,
        claude_md_hash=compute_file_hash(REPO_ROOT / "CLAUDE.md"),
        prompt_file_hashes=_hash_files_in_dir(_AGENTS_DIR, "*.md"),
        quant_module_hash=_hash_directory(_CODE_MODULE_DIRS["quant_module"]),
        risk_module_hash=_hash_directory(_CODE_MODULE_DIRS["risk_module"]),
        paper_broker_module_hash=compute_file_hash(_CODE_MODULE_FILES["paper_broker_module"]),
        market_calendar_module_hash=compute_file_hash(_CODE_MODULE_FILES["market_calendar_module"]),
        market_data_provider_config_version="n/a",
        market_data_provider_config_note=(
            "no separate market-data-provider config YAML file exists -- provider selection "
            "(mock/ibkr/alpaca) is the single OPTIONS_AGENT_DATA_PROVIDER environment variable "
            "(src.data.factory.DataProviderSelection), and canonical quote/chain shape plus "
            "freshness thresholds are defined directly in src/data/provider.py, "
            "src/data/quotes.py, src/data/option_chain.py -- covered by risk_module_hash's "
            "sibling code but not independently hashed here"
        ),
        freeze_version="1.4.8",
        alpaca_provider_module_hash=compute_file_hash(_CODE_MODULE_FILES["alpaca_provider_module"]),
        data_provider_at_freeze_time=_current_data_provider_selection(),
        required_options_feed_for_validation=REQUIRED_OPTIONS_FEED_FOR_VALIDATION,
        wheel_module_hash=_hash_directory(_CODE_MODULE_DIRS["wheel_module"]),
        wheel_strategy_kind_trade_proposal_eligible=_wheel_is_trade_proposal_eligible(),
        lifecycle_module_hash=_hash_directory(_CODE_MODULE_DIRS["lifecycle_module"]),
        lifecycle_named_policy_count=_lifecycle_named_policy_count(),
        tradier_provider_module_hash=compute_file_hash(_CODE_MODULE_FILES["tradier_provider_module"]),
        rate_limiter_module_hash=compute_file_hash(_CODE_MODULE_FILES["rate_limiter_module"]),
        quality_gate_module_hash=compute_file_hash(_CODE_MODULE_FILES["quality_gate_module"]),
        portfolio_module_hash=_hash_directory(_CODE_MODULE_DIRS["portfolio_module"]),
        tradier_market_data_only=_verify_tradier_is_market_data_only(),
        control_loop_cannot_execute_trades=_verify_portfolio_has_no_live_trading_client(),
        smoke_tradier_script_hash=compute_file_hash(_CODE_MODULE_FILES["smoke_tradier_script"]),
        control_loop_projection_module_hash=compute_file_hash(_CODE_MODULE_FILES["control_loop_projection_module"]),
        dashboard_cannot_execute_trades=_verify_dashboard_has_no_live_trading_client(),
        orchestrator_cannot_bypass_risk_or_lifecycle=_verify_orchestrator_does_not_bypass_risk_or_lifecycle(),
        opportunity_scan_never_outranks_risk_monitoring=_verify_opportunity_scan_never_outranks_risk_monitoring(),
        review_module_hash=_hash_directory(_CODE_MODULE_DIRS["review_module"]),
        run_validation_cycle_script_hash=compute_file_hash(_CODE_MODULE_FILES["run_validation_cycle_script"]),
        confirm_candidate_script_hash=compute_file_hash(_CODE_MODULE_FILES["confirm_candidate_script"]),
        daily_cycle_never_calls_place_order=_verify_daily_cycle_never_calls_place_order(),
        review_only_path_never_imports_llm=_verify_review_only_path_never_imports_llm(),
        factory_module_hash=compute_file_hash(_CODE_MODULE_FILES["factory_module"]),
        help_cannot_execute_validation=_verify_help_cannot_execute_validation(),
        official_cycle_requires_tradier_preflight=_verify_official_cycle_requires_tradier_preflight(),
        data_provider_module_hash=compute_file_hash(_CODE_MODULE_FILES["data_provider_module"]),
        dashboard_app_module_hash=compute_file_hash(_CODE_MODULE_FILES["dashboard_app_module"]),
        dashboard_validation_ops_module_hash=compute_file_hash(_CODE_MODULE_FILES["dashboard_validation_ops_module"]),
        dashboard_cannot_confirm_candidates=_verify_dashboard_cannot_confirm_candidates(),
        fill_model_assumptions=dict(
            fill_model=default_paper_cfg.fill_model.value,
            thin_volume_threshold=default_paper_cfg.thin_volume_threshold,
            thin_open_interest_threshold=default_paper_cfg.thin_open_interest_threshold,
            thin_liquidity_penalty_multiplier=default_paper_cfg.thin_liquidity_penalty_multiplier,
            max_fill_fraction_of_volume=default_paper_cfg.max_fill_fraction_of_volume,
            min_guaranteed_fill_contracts=default_paper_cfg.min_guaranteed_fill_contracts,
            max_quote_age_seconds=default_paper_cfg.max_quote_age.total_seconds(),
        ),
        slippage_assumptions=dict(
            slippage_bps=default_paper_cfg.slippage_bps,
            spread_capture_fraction=default_paper_cfg.spread_capture_fraction,
        ),
        commission_assumptions=dict(
            per_contract=default_commission.per_contract,
            per_leg_base=default_commission.per_leg_base,
        ),
        option_multiplier_assumption=100,
        benchmark_definitions=(
            "spy_return: src.backtest.benchmark.spy_total_return over the validation period's exact dates",
            "risk_free_return: src.backtest.benchmark.risk_free_return, annualized rate over the same dates",
            "cash_return: flat 0.0 (holding cash the whole period)",
        ),
        starting_nav_default=validation_config.default_starting_nav,
        validation_duration_days=validation_config.duration_days,
        minimum_sample_size=validation_config.minimum_completed_trades,
        preferred_sample_size=validation_config.preferred_completed_trades,
        drawdown_thresholds=dict(
            warning_pct=float(drawdown.get("warning_pct", 0.0)),
            risk_reduction_pct=float(drawdown.get("risk_reduction_pct", 0.0)),
            halt_pct=float(drawdown.get("halt_pct", 0.0)),
        ),
        research_targets=dict(
            annual_return_low_pct=validation_config.research_target_annual_return_low_pct,
            annual_return_high_pct=validation_config.research_target_annual_return_high_pct,
            reference_min_sharpe=validation_config.research_reference_min_sharpe,
            reference_max_acceptable_drawdown_pct=validation_config.research_reference_max_acceptable_drawdown_pct,
        ),
        database_schema_version=DATABASE_SCHEMA_VERSION,
        report_export_schema_version=EXPORT_SCHEMA_VERSION,
        fidelity_execution_mode="MANUAL_EXECUTION",
        live_trading_enabled=False,
        automatic_fidelity_execution=False,
        validation_cohort_started=False,
    )
    manifest_data["manifest_hash"] = _build_time_manifest_hash(manifest_data)
    return FreezeManifest(**manifest_data)


def save_freeze_manifest(manifest: FreezeManifest, path: Path | str | None = None) -> Path:
    target = Path(path) if path is not None else REPO_ROOT / MANIFEST_FILENAME
    target.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return target


def load_freeze_manifest(path: Path | str | None = None) -> FreezeManifest:
    target = Path(path) if path is not None else REPO_ROOT / MANIFEST_FILENAME
    if not target.is_file():
        raise FreezeManifestError(f"freeze manifest not found: {target}")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FreezeManifestError(f"freeze manifest at {target} is not valid JSON: {exc}") from exc
    return FreezeManifest.model_validate(raw)


def verify_freeze(path: Path | str | None = None) -> FreezeVerificationResult:
    """Part 24's `make verify-freeze`: re-derives every hashable field
    from the current working tree and compares against what
    `VALIDATION_MANIFEST.json` recorded at freeze time. Any mismatch is
    reported as a named, explicit failed check -- never silently
    ignored, never auto-"fixed" by re-freezing on the caller's behalf."""
    checks: list[FreezeCheck] = []

    try:
        manifest = load_freeze_manifest(path)
    except FreezeManifestError as exc:
        return FreezeVerificationResult(
            passed=False, checks=(FreezeCheck(name="manifest_exists", passed=False, detail=str(exc)),),
        )
    checks.append(FreezeCheck(name="manifest_exists", passed=True, detail=f"loaded {manifest.manifest_version}"))

    recomputed_hash = compute_manifest_hash(json.loads(manifest.model_dump_json()))
    hash_ok = recomputed_hash == manifest.manifest_hash
    checks.append(FreezeCheck(
        name="manifest_hash_self_consistent", passed=hash_ok,
        detail="matches" if hash_ok else f"recorded {manifest.manifest_hash[:12]}... != recomputed {recomputed_hash[:12]}...",
    ))

    for name, path_obj in _NAMED_CONFIG_FILES.items():
        recorded = manifest.config_file_hashes.get(name)
        if path_obj is None:
            checks.append(FreezeCheck(name=f"config_hash:{name}", passed=True, detail="not applicable (documented)"))
            continue
        if not path_obj.is_file():
            checks.append(FreezeCheck(name=f"config_hash:{name}", passed=False, detail="file no longer exists"))
            continue
        current = compute_file_hash(path_obj)
        ok = current == recorded
        checks.append(FreezeCheck(
            name=f"config_hash:{name}", passed=ok,
            detail="unchanged" if ok else "DRIFTED since freeze -- material configuration change detected",
        ))

    claude_md = REPO_ROOT / "CLAUDE.md"
    claude_ok = claude_md.is_file() and compute_file_hash(claude_md) == manifest.claude_md_hash
    checks.append(FreezeCheck(name="claude_md_hash", passed=claude_ok, detail="unchanged" if claude_ok else "DRIFTED since freeze"))

    for name, recorded_hash in manifest.prompt_file_hashes.items():
        agent_path = _AGENTS_DIR / name
        ok = agent_path.is_file() and compute_file_hash(agent_path) == recorded_hash
        checks.append(FreezeCheck(name=f"prompt_hash:{name}", passed=ok, detail="unchanged" if ok else "DRIFTED since freeze"))

    module_checks = (
        ("quant_module_hash", _CODE_MODULE_DIRS["quant_module"], True),
        ("risk_module_hash", _CODE_MODULE_DIRS["risk_module"], True),
        ("paper_broker_module_hash", _CODE_MODULE_FILES["paper_broker_module"], False),
        ("market_calendar_module_hash", _CODE_MODULE_FILES["market_calendar_module"], False),
        ("alpaca_provider_module_hash", _CODE_MODULE_FILES["alpaca_provider_module"], False),
        ("wheel_module_hash", _CODE_MODULE_DIRS["wheel_module"], True),
        ("lifecycle_module_hash", _CODE_MODULE_DIRS["lifecycle_module"], True),
        ("tradier_provider_module_hash", _CODE_MODULE_FILES["tradier_provider_module"], False),
        ("rate_limiter_module_hash", _CODE_MODULE_FILES["rate_limiter_module"], False),
        ("quality_gate_module_hash", _CODE_MODULE_FILES["quality_gate_module"], False),
        ("portfolio_module_hash", _CODE_MODULE_DIRS["portfolio_module"], True),
        ("smoke_tradier_script_hash", _CODE_MODULE_FILES["smoke_tradier_script"], False),
        ("control_loop_projection_module_hash", _CODE_MODULE_FILES["control_loop_projection_module"], False),
        ("review_module_hash", _CODE_MODULE_DIRS["review_module"], True),
        ("run_validation_cycle_script_hash", _CODE_MODULE_FILES["run_validation_cycle_script"], False),
        ("confirm_candidate_script_hash", _CODE_MODULE_FILES["confirm_candidate_script"], False),
        ("factory_module_hash", _CODE_MODULE_FILES["factory_module"], False),
        ("data_provider_module_hash", _CODE_MODULE_FILES["data_provider_module"], False),
        ("dashboard_app_module_hash", _CODE_MODULE_FILES["dashboard_app_module"], False),
        ("dashboard_validation_ops_module_hash", _CODE_MODULE_FILES["dashboard_validation_ops_module"], False),
    )
    for field_name, target_path, is_dir in module_checks:
        recorded = getattr(manifest, field_name)
        current = _hash_directory(target_path) if is_dir else compute_file_hash(target_path)
        ok = current == recorded
        checks.append(FreezeCheck(name=field_name, passed=ok, detail="unchanged" if ok else "DRIFTED since freeze"))

    strategy_lib_current = _hash_directory(_CODE_MODULE_DIRS["strategies_module"])
    ok = strategy_lib_current == manifest.strategy_library_version
    checks.append(FreezeCheck(name="strategy_library_version", passed=ok, detail="unchanged" if ok else "DRIFTED since freeze"))

    db_ok = manifest.database_schema_version == DATABASE_SCHEMA_VERSION
    checks.append(FreezeCheck(
        name="database_schema_version", passed=db_ok,
        detail=f"{manifest.database_schema_version} == current {DATABASE_SCHEMA_VERSION}" if db_ok
        else f"manifest records {manifest.database_schema_version!r}, current code defines {DATABASE_SCHEMA_VERSION!r}",
    ))

    brokers_cfg = _load_yaml(_CONFIG_DIR / "brokers.yaml")
    current_fidelity_mode = str(brokers_cfg.get("fidelity", {}).get("execution_mode", ""))
    fidelity_ok = current_fidelity_mode == "MANUAL" and manifest.fidelity_execution_mode == "MANUAL_EXECUTION"
    checks.append(FreezeCheck(
        name="fidelity_manual_execution_only", passed=fidelity_ok,
        detail="confirmed MANUAL" if fidelity_ok else f"current config/brokers.yaml fidelity.execution_mode={current_fidelity_mode!r}",
    ))

    from src.brokers.base import BrokerEnvironment
    live_disabled = (not manifest.live_trading_enabled) and set(BrokerEnvironment) == {BrokerEnvironment.PAPER}
    checks.append(FreezeCheck(
        name="live_trading_disabled", passed=live_disabled,
        detail="BrokerEnvironment has only PAPER; manifest.live_trading_enabled=False" if live_disabled
        else "BrokerEnvironment now has a non-PAPER member, or manifest claims live trading enabled",
    ))

    auto_fidelity_ok = manifest.automatic_fidelity_execution is False
    checks.append(FreezeCheck(
        name="automatic_fidelity_execution_disabled", passed=auto_fidelity_ok,
        detail="False, as required" if auto_fidelity_ok else "manifest records True -- CLAUDE.md invariant #3 violated",
    ))

    cohort_ok = manifest.validation_cohort_started is False
    checks.append(FreezeCheck(
        name="validation_cohort_not_started", passed=cohort_ok,
        detail="False, as required (90-day validation has not started)" if cohort_ok
        else "manifest records True -- this freeze illegally claims validation already started",
    ))

    alpaca_market_data_only_ok = _verify_alpaca_is_market_data_only()
    checks.append(FreezeCheck(
        name="alpaca_market_data_only", passed=alpaca_market_data_only_ok,
        detail="no alpaca.trading import found anywhere in src/" if alpaca_market_data_only_ok
        else "an alpaca.trading import was found in src/ -- Alpaca must remain market-data-only",
    ))

    wheel_no_live_client_ok = _verify_wheel_has_no_live_trading_client()
    checks.append(FreezeCheck(
        name="wheel_no_live_trading_client", passed=wheel_no_live_client_ok,
        detail="no live trading-client import found anywhere in src/wheel/" if wheel_no_live_client_ok
        else "a live trading-client import was found in src/wheel/ -- the Wheel must remain "
        "PaperBroker/Fidelity-manual-ticket only",
    ))

    wheel_not_eligible_ok = _wheel_is_trade_proposal_eligible() is False and manifest.wheel_strategy_kind_trade_proposal_eligible is False
    checks.append(FreezeCheck(
        name="wheel_never_becomes_its_own_order_type", passed=wheel_not_eligible_ok,
        detail="StrategyKind.WHEEL absent from TRADE_PROPOSAL_ELIGIBLE, as required" if wheel_not_eligible_ok
        else "StrategyKind.WHEEL has become TRADE_PROPOSAL_ELIGIBLE or the manifest wrongly claims it -- "
        "every Wheel order must remain an ordinary CASH_SECURED_PUT/COVERED_CALL TradeProposal",
    ))

    lifecycle_no_live_client_ok = _verify_lifecycle_has_no_live_trading_client()
    checks.append(FreezeCheck(
        name="lifecycle_no_live_trading_client", passed=lifecycle_no_live_client_ok,
        detail="no live trading-client import found anywhere in src/lifecycle/" if lifecycle_no_live_client_ok
        else "a live trading-client import was found in src/lifecycle/ -- lifecycle actions must remain "
        "PaperBroker/Fidelity-manual-ticket only",
    ))

    lifecycle_policy_count_current = _lifecycle_named_policy_count()
    lifecycle_policy_count_ok = lifecycle_policy_count_current >= 19 and manifest.lifecycle_named_policy_count >= 19
    checks.append(FreezeCheck(
        name="lifecycle_named_policy_count", passed=lifecycle_policy_count_ok,
        detail=f"{lifecycle_policy_count_current} named policies (>= 19, covering every StrategyKind)" if lifecycle_policy_count_ok
        else f"only {lifecycle_policy_count_current} named lifecycle policies found -- below the 19-policy floor "
        "this freeze recorded (every StrategyKind must have at least one named research policy)",
    ))

    tradier_market_data_only_ok = _verify_tradier_is_market_data_only() and manifest.tradier_market_data_only
    checks.append(FreezeCheck(
        name="tradier_market_data_only", passed=tradier_market_data_only_ok,
        detail="no Tradier order/trading-shaped identifier found anywhere in src/, and manifest records True"
        if tradier_market_data_only_ok
        else "a Tradier order/trading-shaped identifier was found in src/, or the manifest wrongly claims "
        "market-data-only status -- Tradier must remain read-only GET-only market data",
    ))

    control_loop_no_live_client_ok = (
        _verify_portfolio_has_no_live_trading_client() and manifest.control_loop_cannot_execute_trades
    )
    checks.append(FreezeCheck(
        name="control_loop_cannot_execute_trades", passed=control_loop_no_live_client_ok,
        detail="no live trading-client import and no order-submission method name found anywhere in "
        "src/portfolio/, and manifest records True" if control_loop_no_live_client_ok
        else "a live trading-client import or order-submission method name was found in src/portfolio/, "
        "or the manifest wrongly claims the control loop cannot execute trades -- the Portfolio Control "
        "Loop must remain a read-only orchestrator over the existing Risk Engine/PaperBroker/Fidelity paths",
    ))

    dashboard_no_live_client_ok = (
        _verify_dashboard_has_no_live_trading_client() and manifest.dashboard_cannot_execute_trades
    )
    checks.append(FreezeCheck(
        name="dashboard_cannot_execute_trades", passed=dashboard_no_live_client_ok,
        detail="no live trading-client import and no order-submission method name found anywhere in "
        "src/dashboard/, and manifest records True" if dashboard_no_live_client_ok
        else "a live trading-client import or order-submission method name was found in src/dashboard/, "
        "or the manifest wrongly claims the dashboard cannot execute trades -- every dashboard route "
        "must remain read-only over the existing Risk Engine/Fidelity-manual-ticket paths",
    ))

    orchestrator_no_bypass_ok = (
        _verify_orchestrator_does_not_bypass_risk_or_lifecycle() and manifest.orchestrator_cannot_bypass_risk_or_lifecycle
    )
    checks.append(FreezeCheck(
        name="orchestrator_cannot_bypass_risk_or_lifecycle", passed=orchestrator_no_bypass_ok,
        detail="no direct src.risk.engine/src.lifecycle.engine import and no confirm_fill call found in "
        "src/portfolio/orchestrator.py, and manifest records True" if orchestrator_no_bypass_ok
        else "src/portfolio/orchestrator.py directly imports src.risk.engine/src.lifecycle.engine or calls "
        "confirm_fill, or the manifest wrongly claims otherwise -- the outer orchestrator must only ever "
        "reach the Risk Engine/Lifecycle Engine/Fidelity fill-confirmation through the existing, unmodified "
        "run_control_cycle/scan_and_rank_opportunities/monitor_pending_tickets functions",
    ))

    priority_ok = (
        _verify_opportunity_scan_never_outranks_risk_monitoring() and manifest.opportunity_scan_never_outranks_risk_monitoring
    )
    checks.append(FreezeCheck(
        name="opportunity_scan_never_outranks_risk_monitoring", passed=priority_ok,
        detail="RateLimitPriority.P4_OPPORTUNITY_SCANNING > P3_PENDING_TICKET_REPRICING > P0_POSITION_RISK "
        "holds, and manifest records True" if priority_ok
        else "the rate-limit priority ordering no longer places new-opportunity scanning strictly below "
        "pending-ticket and existing-position risk monitoring, or the manifest wrongly claims otherwise",
    ))

    daily_cycle_no_place_order_ok = (
        _verify_daily_cycle_never_calls_place_order() and manifest.daily_cycle_never_calls_place_order
    )
    checks.append(FreezeCheck(
        name="daily_cycle_never_calls_place_order", passed=daily_cycle_no_place_order_ok,
        detail="'place_order' not found in scripts/run_validation_cycle.py, and manifest records True"
        if daily_cycle_no_place_order_ok
        else "'place_order' was found in scripts/run_validation_cycle.py, or the manifest wrongly claims "
        "otherwise -- the unattended daily cycle must never open a new PaperBroker position; only "
        "scripts/confirm_candidate.py may",
    ))

    review_only_no_llm_ok = (
        _verify_review_only_path_never_imports_llm() and manifest.review_only_path_never_imports_llm
    )
    checks.append(FreezeCheck(
        name="review_only_path_never_imports_llm", passed=review_only_no_llm_ok,
        detail="no src.llm import (other than src.llm.schemas) found in scripts/run_validation_cycle.py, "
        "scripts/confirm_candidate.py, or src/review/, and manifest records True" if review_only_no_llm_ok
        else "an src.llm import (other than src.llm.schemas) was found on the Review-Only new-position "
        "execution path, or the manifest wrongly claims otherwise -- no real or faked LLM review may "
        "ever occur there",
    ))

    help_safety_ok = _verify_help_cannot_execute_validation() and manifest.help_cannot_execute_validation
    checks.append(FreezeCheck(
        name="help_cannot_execute_validation", passed=help_safety_ok,
        detail="scripts/run_validation_cycle.py's main() parses CLI arguments (argparse) before its first "
        "mutating call, and manifest records True" if help_safety_ok
        else "scripts/run_validation_cycle.py's main() no longer demonstrably parses CLI arguments before a "
        "mutating call, or the manifest wrongly claims otherwise -- python scripts/run_validation_cycle.py "
        "--help must never run an official validation cycle",
    ))

    official_provider_preflight_ok = (
        _verify_official_cycle_requires_tradier_preflight() and manifest.official_cycle_requires_tradier_preflight
    )
    checks.append(FreezeCheck(
        name="official_cycle_requires_tradier_preflight", passed=official_provider_preflight_ok,
        detail="scripts/run_validation_cycle.py calls verify_official_provider_is_tradier_production before "
        "every one of its known mutating calls, and manifest records True" if official_provider_preflight_ok
        else "scripts/run_validation_cycle.py no longer demonstrably verifies Tradier production provider "
        "configuration before a mutating call, or the manifest wrongly claims otherwise -- the official cycle "
        "must never mutate validation state while configured for mock/synthetic/non-Tradier-production data",
    ))

    dashboard_confirm_ok = (
        _verify_dashboard_cannot_confirm_candidates() and manifest.dashboard_cannot_confirm_candidates
    )
    checks.append(FreezeCheck(
        name="dashboard_cannot_confirm_candidates", passed=dashboard_confirm_ok,
        detail="no src.dashboard file imports src.review.confirmation or confirm_candidate, and manifest "
        "records True" if dashboard_confirm_ok
        else "a src.dashboard file now imports src.review.confirmation/confirm_candidate, or the manifest "
        "wrongly claims otherwise -- confirming a Review-Only candidate must always stay a separate, explicit "
        "scripts/confirm_candidate.py command, never a dashboard action",
    ))

    passed = all(c.passed for c in checks)
    return FreezeVerificationResult(passed=passed, checks=tuple(checks))


def _verify_alpaca_is_market_data_only() -> bool:
    """Step 22.1: a direct, executable proof (not just a file hash) that
    no file under `src/` imports `alpaca.trading` (Alpaca's
    order-submission client) -- re-checked on every `verify_freeze` run,
    independent of `tests/acceptance/test_alpaca_market_data_only.py`."""
    import re

    pattern = re.compile(r"^\s*(from|import)\s+alpaca\.trading\b", re.MULTILINE)
    for path in (REPO_ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if pattern.search(path.read_text(errors="ignore")):
            return False
    return True


def _verify_wheel_has_no_live_trading_client() -> bool:
    """Step 22.2: a direct, executable proof (not just a directory hash)
    that no file under `src/wheel/` imports a live trading client
    (Alpaca's order-submission client, ib_insync, ibapi) -- re-checked
    on every `verify_freeze` run, independent of
    `tests/acceptance/test_wheel_security.py`."""
    import re

    pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
    for path in (REPO_ROOT / "src" / "wheel").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if pattern.search(path.read_text(errors="ignore")):
            return False
    return True


def _verify_lifecycle_has_no_live_trading_client() -> bool:
    """Step 22.3: a direct, executable proof (not just a directory hash)
    that no file under `src/lifecycle/` imports a live trading client
    (Alpaca's order-submission client, ib_insync, ibapi) -- re-checked
    on every `verify_freeze` run, independent of
    `tests/acceptance/test_lifecycle_security.py`."""
    import re

    pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
    for path in (REPO_ROOT / "src" / "lifecycle").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if pattern.search(path.read_text(errors="ignore")):
            return False
    return True


def _verify_tradier_is_market_data_only() -> bool:
    """Step 22.4: a direct, executable proof (not just a file hash) that
    no file under `src/` references a Tradier order/trading-shaped
    identifier (`TradierBroker`, `TradierOrderClient`,
    `TradierExecutionProvider`) or an accounts-orders endpoint path --
    re-checked on every `verify_freeze` run, independent of
    `tests/acceptance/test_tradier_market_data_only.py`."""
    import re

    class_pattern = re.compile(r"Tradier(Broker|Order(Client|Provider)?|ExecutionProvider)\b")
    endpoint_pattern = re.compile(r"/v1/accounts/[^\"'\s]*/orders", re.IGNORECASE)
    docstring_pattern = re.compile(r'"""[\s\S]*?"""')
    # This module's own docstrings document-by-name the identifiers this
    # check forbids (explaining the prohibition) -- exempted from the
    # literal substring match, its code is still scanned. The dedicated
    # security acceptance test is a *second*, independent proof of the
    # same property (its own code literally asserts these forbidden
    # strings are absent, which would otherwise self-trigger this check)
    # -- excluded entirely here, exactly as it excludes itself from its
    # own repo-wide scan for the identical reason. Its own unit-test
    # counterpart (`tests/unit/validation/test_freeze.py`) writes one of
    # the same forbidden order-shaped class names, deliberately, into a
    # temporary poison file -- in actual test code rather than a
    # docstring -- to prove this very check catches drift. Excluded for
    # the identical reason.
    security_test = (REPO_ROOT / "tests" / "acceptance" / "test_tradier_market_data_only.py").resolve()
    freeze_unit_test = (REPO_ROOT / "tests" / "unit" / "validation" / "test_freeze.py").resolve()
    this_file = (REPO_ROOT / "src" / "validation" / "freeze.py").resolve()

    for path in (REPO_ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        code = docstring_pattern.sub("", text) if path.resolve() in (this_file, REPO_ROOT / "src" / "data" / "tradier_provider.py") else text
        if class_pattern.search(code) or endpoint_pattern.search(code):
            return False
    for path in REPO_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or ".git" in path.parts or path.resolve() in (security_test, freeze_unit_test):
            continue
        text = path.read_text(errors="ignore")
        code = docstring_pattern.sub("", text) if path.resolve() == this_file else text
        if class_pattern.search(code):
            return False
    return True


def _verify_portfolio_has_no_live_trading_client() -> bool:
    """Step 22.4: a direct, executable proof (not just a directory hash)
    that no file under `src/portfolio/` imports a live trading client
    (Alpaca's order-submission client, ib_insync, ibapi) or calls an
    order-submission-shaped method name -- re-checked on every
    `verify_freeze` run, the same methodology
    `_verify_wheel_has_no_live_trading_client`/
    `_verify_lifecycle_has_no_live_trading_client` already establish."""
    import re

    import_pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
    order_method_pattern = re.compile(
        r"\b(place_order|submit_order|cancel_order|modify_order|amend_order|"
        r"preview_order|replace_order|submit_trade|execute_trade|place_trade|send_order)\s*\(",
        re.IGNORECASE,
    )
    for path in (REPO_ROOT / "src" / "portfolio").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        if import_pattern.search(text) or order_method_pattern.search(text):
            return False
    return True


def _verify_dashboard_has_no_live_trading_client() -> bool:
    """Step 22.4A: a direct, executable proof (not just a file hash)
    that no file under `src/dashboard/` imports a live trading client
    (Alpaca's order-submission client, ib_insync, ibapi) or calls an
    order-submission-shaped method name -- re-checked on every
    `verify_freeze` run, the same methodology
    `_verify_portfolio_has_no_live_trading_client` already establishes,
    applied to the dashboard's new control-loop projection surface."""
    import re

    import_pattern = re.compile(r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE)
    # `cancel_order` is deliberately excluded from this vocabulary here
    # (unlike `_verify_portfolio_has_no_live_trading_client`'s identical
    # list, where no such collision exists): `src.dashboard.service
    # .cancel_order`/the matching `app.py` route are Step 18's own
    # pre-existing, legitimate CANCELLED action -- pulling back a
    # Fidelity ticket that was already manually entered, purely through
    # the existing `transition()` state machine, never a live
    # broker-order-cancellation API call. That capability is already
    # independently, exhaustively proven safe by
    # `tests/unit/dashboard/test_app_security.py`'s route-inventory and
    # no-credential tests; this check instead targets the genuinely
    # order-placement-shaped vocabulary no legitimate dashboard action
    # is ever named after.
    order_method_pattern = re.compile(
        r"\b(place_order|submit_order|modify_order|amend_order|"
        r"preview_order|replace_order|submit_trade|execute_trade|place_trade|send_order)\s*\(",
        re.IGNORECASE,
    )
    for path in (REPO_ROOT / "src" / "dashboard").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        if import_pattern.search(text) or order_method_pattern.search(text):
            return False
    return True


def _verify_orchestrator_does_not_bypass_risk_or_lifecycle() -> bool:
    """Step 22.4A: a direct, executable proof that
    `src/portfolio/orchestrator.py` -- the new outer production
    orchestrator -- never imports `src.risk.engine`/`src.lifecycle.engine`
    directly (it must only ever reach them indirectly, through the
    existing, unmodified `run_control_cycle`/`scan_and_rank_opportunities`)
    and never calls `confirm_fill` (the one function anywhere in this
    codebase that can transition a Fidelity ticket to FILLED --
    a Risk approval or a scanned opportunity must never be silently
    treated as an executed fill)."""
    import re

    path = REPO_ROOT / "src" / "portfolio" / "orchestrator.py"
    if not path.is_file():
        return False
    text = path.read_text(errors="ignore")
    import_pattern = re.compile(r"^\s*(from|import)\s+src\.(risk\.engine|lifecycle\.engine)\b", re.MULTILINE)
    if import_pattern.search(text):
        return False
    if "confirm_fill" in text:
        return False
    return True


def _verify_opportunity_scan_never_outranks_risk_monitoring() -> bool:
    """Step 22.4A Part 10/14: re-reads the orchestrator's own
    `RateLimitPriority` constants at verify time (not a hardcoded
    assumption) and confirms new-opportunity scanning is strictly the
    lowest of the three -- P4 (opportunity scanning) > P3 (pending-ticket
    monitoring) > P0 (existing-position risk monitoring, never gated by
    rate limit in this module at all, see its own module docstring)."""
    from src.data.rate_limiter import RateLimitPriority
    from src.portfolio.orchestrator import _OPPORTUNITY_SCAN_PRIORITY, _TICKET_MONITOR_PRIORITY

    return (
        _OPPORTUNITY_SCAN_PRIORITY > _TICKET_MONITOR_PRIORITY
        and _TICKET_MONITOR_PRIORITY > RateLimitPriority.P0_POSITION_RISK
    )


def _verify_daily_cycle_never_calls_place_order() -> bool:
    """Step 22.5: a direct, executable proof (not just a file hash) that
    `scripts/run_validation_cycle.py` -- the unattended daily runner --
    never calls `PaperBroker.place_order` for a new position. The only
    function anywhere in this codebase permitted to do that is
    `src.review.confirmation.confirm_candidate`, invoked exclusively from
    the separate, human-run `scripts/confirm_candidate.py`. Checks for
    the literal call shape `place_order(` (an open paren immediately
    after) rather than the bare substring `place_order`, which this
    script's own module docstring and operator-facing print statement
    both legitimately mention *by name* to explain the very invariant
    this function verifies -- neither is followed by a `(`, so neither
    trips this check. Independent of (and re-checked on every
    `verify_freeze` run alongside)
    `tests/acceptance/test_review_only_daily_cycle.py`'s own behavioral
    proof."""
    path = REPO_ROOT / "scripts" / "run_validation_cycle.py"
    if not path.is_file():
        return False
    return "place_order(" not in path.read_text(errors="ignore")


def _verify_review_only_path_never_imports_llm() -> bool:
    """Step 22.5: a direct, executable proof that the Review-Only
    new-position execution path (`scripts/run_validation_cycle.py`,
    `scripts/confirm_candidate.py`, and every module under `src/review/`)
    never imports from `src.llm` -- except `src.llm.schemas`, the
    platform-wide home of plain, immutable `TradeProposal`/`StrategyType`
    data shapes with no LLM call anywhere in them (CLAUDE.md invariant #2
    is exactly why they're categorical/read-only in the first place),
    already imported unconditionally by every deterministic layer in this
    codebase (`src.risk.engine`, `src.portfolio.opportunity_scan`,
    `src.workflows.candidate_generation`, among others). No real
    Anthropic API call, and no faked deterministic Devil's Advocate/
    Portfolio Manager stand-in, is ever reachable from this path.
    Independent of (and re-checked on every `verify_freeze` run
    alongside) `tests/acceptance/test_no_llm_in_review_only_path.py`'s
    own AST-based proof."""
    import ast

    allowed = {"src.llm.schemas"}
    targets = (
        REPO_ROOT / "scripts" / "run_validation_cycle.py",
        REPO_ROOT / "scripts" / "confirm_candidate.py",
        *sorted((REPO_ROOT / "src" / "review").glob("*.py")),
    )
    for path in targets:
        if not path.is_file():
            return False
        try:
            tree = ast.parse(path.read_text(errors="ignore"), filename=str(path))
        except SyntaxError:
            # A file this broken is drift by definition -- fail closed
            # rather than let an unhandled exception blow past every
            # other check `verify_freeze` still owes the caller.
            return False
        for node in ast.walk(tree):
            module_name = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "src.llm" or alias.name.startswith("src.llm."):
                        module_name = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "src.llm" or node.module.startswith("src.llm."):
                    module_name = node.module
            if module_name is not None and module_name not in allowed:
                return False
    return True


def _extract_function_body(text: str, start_marker: str) -> str | None:
    """Returns the source text strictly between `start_marker` (a
    function's own signature line, e.g. `"\\ndef main() -> int:\\n"`) and
    the next top-level (column-0) `def`/`async def` -- i.e. just that one
    function's body, excluding every other function's own definition
    (including ones whose *name* happens to be a substring this module
    searches for elsewhere, e.g. a helper's `def` line containing the
    same call-shaped text its own callers use). `None` if `start_marker`
    isn't found at all."""
    import re

    start = text.find(start_marker)
    if start == -1:
        return None
    body_start = start + len(start_marker)
    rest = text[body_start:]
    match = re.search(r"\n(?:async )?def ", rest)
    end = body_start + match.start() if match else len(text)
    return text[body_start:end]


def _verify_help_cannot_execute_validation() -> bool:
    """Step 22.6: a direct, executable proof (not just a docstring claim)
    that `scripts/run_validation_cycle.py`'s `main()` parses its CLI
    arguments (`argparse`) before it can reach either of its two mutating
    calls (`run_validation_cycle()`/`run_preflight()`). Scoped to `main()`'s
    own body only (`_extract_function_body`) -- a naive whole-file
    substring search would instead match this very module's own
    docstring (which explains the invariant by quoting `parser
    .parse_args()`) or `run_validation_cycle`'s own `def` line (which
    appears earlier in the file than `main()` regardless of what `main()`
    actually does) and could pass for the wrong reason. Within `main()`'s
    body, checks that `.parse_args(` appears strictly before both
    `run_validation_cycle()` and `run_preflight()` are ever called --
    `argparse` itself calls `sys.exit()` for `-h`/`--help` and for an
    unrecognized argument, so parsing strictly first is what guarantees
    neither case ever reaches a mutating call. Independent of (and
    re-checked on every `verify_freeze` run alongside)
    `tests/acceptance/test_run_validation_cycle_cli.py`'s own behavioral
    (subprocess-based) proof."""
    path = REPO_ROOT / "scripts" / "run_validation_cycle.py"
    if not path.is_file():
        return False
    text = path.read_text(errors="ignore")
    if "argparse.ArgumentParser" not in text:
        return False
    main_body = _extract_function_body(text, "\ndef main() -> int:\n")
    if main_body is None:
        return False
    parse_args_index = main_body.find(".parse_args(")
    if parse_args_index == -1:
        return False
    for call in ("run_validation_cycle()", "run_preflight()"):
        call_index = main_body.find(call)
        if call_index == -1:
            continue  # this branch of main() doesn't call it -- nothing to order here
        if parse_args_index > call_index:
            return False
    return True


def _verify_official_cycle_requires_tradier_preflight() -> bool:
    """Step 22.6: a direct, executable proof that
    `scripts/run_validation_cycle.py` calls
    `verify_official_provider_is_tradier_production` strictly before
    every one of its own known mutating calls (candidate expiry, the
    Portfolio/PaperAccountState bootstrap-and-save calls, and the daily
    snapshot record) -- scoped to `run_validation_cycle()`'s own body
    only (`_extract_function_body`), since some of those mutating call
    names are also substrings of a helper function's own `def` line
    (`_expire_stale_candidates`) or appear a second time inside that
    helper's own body (`review_store.save_candidate`), both of which sit
    earlier in the file regardless of this function's actual call
    ordering and would otherwise make a naive whole-file search
    unreliable. Independent of (and re-checked on every `verify_freeze`
    run alongside) `tests/acceptance/test_run_validation_cycle_cli.py`'s
    own behavioral proof that a `mock`-configured environment is refused
    before any validation state is touched."""
    path = REPO_ROOT / "scripts" / "run_validation_cycle.py"
    if not path.is_file():
        return False
    text = path.read_text(errors="ignore")
    body = _extract_function_body(text, "\nasync def run_validation_cycle() -> bool:\n")
    if body is None:
        return False
    preflight_index = body.find("verify_official_provider_is_tradier_production(")
    if preflight_index == -1:
        return False
    known_mutating_calls = (
        "_expire_stale_candidates(",
        "portfolio_store.save(",
        "account_state_store.save(",
        "review_store.save_candidate(",
        "validation_store.record_snapshot(",
    )
    for call in known_mutating_calls:
        call_index = body.find(call)
        if call_index == -1:
            return False
        if preflight_index > call_index:
            return False
    return True


def _verify_dashboard_cannot_confirm_candidates() -> bool:
    """Step 22.8: a direct, executable proof (not just a docstring
    claim) that no file under `src/dashboard/` imports
    `src.review.confirmation` or the `confirm_candidate` module --
    confirming a Review-Only candidate must always stay a separate,
    deliberate `scripts/confirm_candidate.py` operator command, never
    something reachable from a dashboard route. Scoped to actual
    `import`/`from ... import` statement lines, not whole-file
    substring matching -- both `app.py` and `validation_ops.py`
    legitimately explain, in prose, that confirmation stays CLI-only,
    which names these exact identifiers to describe their own absence
    and would otherwise trip a naive scan. Independent of (and
    re-checked on every `verify_freeze` run alongside)
    `tests/unit/dashboard/test_operator_status.py`'s own structural
    proof."""
    import re

    import_pattern = re.compile(r"^\s*(?:import|from)\s+\S*(?:review\.confirmation|confirm_candidate)\S*", re.MULTILINE)
    dashboard_dir = REPO_ROOT / "src" / "dashboard"
    if not dashboard_dir.is_dir():
        return False
    for path in dashboard_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        if import_pattern.search(text):
            return False
    return True


def _cli_build() -> int:
    manifest = build_freeze_manifest()
    path = save_freeze_manifest(manifest)
    print(f"Wrote {path} (manifest_hash={manifest.manifest_hash})")
    return 0


def _cli_verify() -> int:
    result = verify_freeze()
    for check in result.checks:
        mark = "OK  " if check.passed else "FAIL"
        print(f"[{mark}] {check.name}: {check.detail}")
    print()
    if result.passed:
        print(f"{FREEZE_NAME} / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR FINAL PRE-VALIDATION ACCEPTANCE")
        return 0
    print(f"{FREEZE_NAME} / FREEZE VERIFICATION FAILED -- {len(result.failures)} check(s) failed -- see above")
    return 1


if __name__ == "__main__":
    import sys as _sys

    _command = _sys.argv[1] if len(_sys.argv) > 1 else "verify"
    if _command == "build":
        raise SystemExit(_cli_build())
    elif _command == "verify":
        raise SystemExit(_cli_verify())
    else:
        print(f"usage: python -m src.validation.freeze [build|verify]", file=_sys.stderr)
        raise SystemExit(2)
