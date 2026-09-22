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
# strategy amendment), and Step 22.3 (Strategy Lifecycle Management
# Engine amendment) each bumped the freeze name/version in place without
# touching the prior versions' own artifacts -- see progress.md and
# STEP_22_1_FREEZE_REPORT.md / STEP_22_2_FREEZE_REPORT.md /
# STEP_22_3_FREEZE_REPORT.md. FREEZE_NAME/MANIFEST_VERSION always reflect
# the *current* frozen state; the original V1.0/V1.1/V1.2 manifests/
# reports remain recoverable from git history at the
# `paper-trading-v1.0` / `paper-trading-v1.1` / `paper-trading-v1.2` tags.
FREEZE_NAME = "PAPER_TRADING_V1.3"
MANIFEST_FILENAME = "VALIDATION_MANIFEST.json"
MANIFEST_VERSION = "1.3.0"

_CONFIG_DIR = REPO_ROOT / "config"
_AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

# The config files that actually exist in this repository as of Step 22.
# Part 21 also names `strategies.yaml` and `universe.yaml` -- this
# platform never split those out into their own files (per-strategy
# behavior lives in `src/strategies/*.py`, hashed below as code; the
# tradable universe is not yet config-driven at all -- see
# ARCHITECTURE.md), so those two keys are recorded as explicit
# "not applicable" entries rather than silently omitted.
_NAMED_CONFIG_FILES: dict[str, Path | None] = {
    "risk_limits.yaml": _CONFIG_DIR / "risk_limits.yaml",
    "brokers.yaml": _CONFIG_DIR / "brokers.yaml",
    "validation.yaml": _CONFIG_DIR / "validation.yaml",
    "llm.yaml": _CONFIG_DIR / "llm.yaml",
    "strategies.yaml": None,  # not applicable -- see module docstring
    "universe.yaml": None,  # not applicable -- see module docstring
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
}
_CODE_MODULE_FILES: dict[str, Path] = {
    "paper_broker_module": REPO_ROOT / "src" / "brokers" / "paper.py",
    "market_calendar_module": REPO_ROOT / "src" / "data" / "market_calendar.py",
    # Step 22.1: Alpaca is market-data-only (never execution) -- hashing
    # this file means any future change to it (including one that tried
    # to add order-submission capability) is caught as material drift.
    "alpaca_provider_module": REPO_ROOT / "src" / "data" / "alpaca_provider.py",
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
        freeze_version="1.3",
        alpaca_provider_module_hash=compute_file_hash(_CODE_MODULE_FILES["alpaca_provider_module"]),
        data_provider_at_freeze_time=_current_data_provider_selection(),
        required_options_feed_for_validation=REQUIRED_OPTIONS_FEED_FOR_VALIDATION,
        wheel_module_hash=_hash_directory(_CODE_MODULE_DIRS["wheel_module"]),
        wheel_strategy_kind_trade_proposal_eligible=_wheel_is_trade_proposal_eligible(),
        lifecycle_module_hash=_hash_directory(_CODE_MODULE_DIRS["lifecycle_module"]),
        lifecycle_named_policy_count=_lifecycle_named_policy_count(),
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
        print(f"{FREEZE_NAME} / FREEZE VERIFIED / VALIDATION NOT STARTED / READY FOR VALIDATION INITIALIZATION")
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
