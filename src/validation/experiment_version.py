"""PAPER_TRADING_V1.5.0, Step 1: experiment-version metadata foundation.

**What this module is for.** As the daily validation cycle's opportunity
set eventually broadens (more tickers, more candidate-generation
strategies, wired-up risk-data checks -- see the V1.5 architecture plan),
a `TradeRecord`/`ControlCycleRecord`/`DailySnapshot` produced under one
configuration must never be silently indistinguishable from one produced
under a materially different configuration. `ExperimentVersion` is the
single, shared, content-addressed identity object every future record can
optionally reference by id -- never duplicated per record, never
fabricated for a record that predates it.

**What this module is deliberately NOT (Step 1 scope).** It does not
change candidate generation, the ticker universe, active strategies,
Quant, Risk, sizing, lifecycle, Tradier/PaperBroker/Fidelity behavior,
human confirmation, the dashboard, or market-hours behavior. It is not
wired into `scripts/run_validation_cycle.py` or any live cohort by this
step -- nothing here is called by the operational daily cycle yet. It
exists as a tested, standalone metadata foundation; actual wiring is a
later, separate V1.5 step.

**Determinism, not a random UUID.** `version_id` is a SHA-256 hex digest
over a canonical (sorted-key) JSON encoding of every field that defines
the experiment's *identity* -- the software freeze version, the three
protected config file hashes, the strategy-activation stage, and the
market-data configuration hash. `recorded_at` is deliberately excluded
from that digest: two `build_experiment_version()` calls against the
byte-identical configuration produce the byte-identical `version_id`
regardless of when each call happened, so two independently-recorded
"same experiment" instants are recognized as the same experiment rather
than accidentally appearing to be two different ones.

**No secrets, ever, structurally.** This module never reads an
environment variable, never imports `src.data.factory`/
`src.data.tradier_provider`/any provider client, and never opens `.env`.
`compute_market_data_config_hash` accepts only a plain provider name
string and a production/sandbox boolean -- both already-non-secret by
construction (the provider name is a config *choice*, not a credential;
the production/sandbox flag is a policy state, not a credential) -- and
hashes only those two values. There is no code path in this module
capable of hashing, storing, or logging a token, API key, or base URL.

**Reuses, never duplicates, the existing hashing primitive.**
`compute_file_hash` is imported unmodified from `src.validation.protocol`
(the same helper `StrategyVersionManifest`'s own `config_file_hashes`
already uses) -- a config file's hash is computed identically everywhere
in this codebase, never by a second, independently-written hasher that
could silently drift from the first.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.validation.protocol import compute_file_hash

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE_CONFIG_PATH = _REPO_ROOT / "config" / "universe.yaml"
DEFAULT_RISK_CONFIG_PATH = _REPO_ROOT / "config" / "risk_limits.yaml"
DEFAULT_VALIDATION_CONFIG_PATH = _REPO_ROOT / "config" / "validation.yaml"


@dataclass(frozen=True)
class ExperimentVersion:
    """One experiment configuration's frozen, content-addressed identity.

    Every field except `recorded_at` participates in `version_id`'s
    digest (see `compute_experiment_version_id`) -- `recorded_at` is
    purely a "when was this instance first computed" timestamp, never
    part of the configuration's identity itself."""

    version_id: str
    software_freeze_version: str
    universe_config_hash: str
    strategy_activation_stage: str
    risk_config_hash: str
    validation_config_hash: str
    market_data_config_hash: str
    recorded_at: datetime


def compute_market_data_config_hash(*, provider: str, is_production: bool) -> str:
    """Hashes ONLY the market-data provider's name and production/
    sandbox designation -- never a token, base URL, or any other
    credential-shaped value. Both inputs are plain, already-non-secret
    configuration choices; this function has no code path that could
    ever receive or hash a secret, by construction (it takes exactly
    these two parameters and nothing else)."""
    payload = f"provider={provider.strip().lower()}|is_production={bool(is_production)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_experiment_version_id(
    *,
    software_freeze_version: str,
    universe_config_hash: str,
    strategy_activation_stage: str,
    risk_config_hash: str,
    validation_config_hash: str,
    market_data_config_hash: str,
) -> str:
    """Deterministic, content-addressed identity for one experiment
    configuration. `recorded_at` is deliberately not a parameter here --
    it must never influence identity (two calls against the same
    configuration, made at two different times, must produce the same
    id). Canonical JSON (sorted keys, no whitespace) guarantees the same
    logical input always serializes to the same bytes before hashing."""
    identity = {
        "software_freeze_version": software_freeze_version,
        "universe_config_hash": universe_config_hash,
        "strategy_activation_stage": strategy_activation_stage,
        "risk_config_hash": risk_config_hash,
        "validation_config_hash": validation_config_hash,
        "market_data_config_hash": market_data_config_hash,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_experiment_version(
    *,
    software_freeze_version: str,
    strategy_activation_stage: str,
    market_data_provider: str,
    market_data_is_production: bool,
    recorded_at: datetime,
    universe_config_path: Path | str = DEFAULT_UNIVERSE_CONFIG_PATH,
    risk_config_path: Path | str = DEFAULT_RISK_CONFIG_PATH,
    validation_config_path: Path | str = DEFAULT_VALIDATION_CONFIG_PATH,
) -> ExperimentVersion:
    """Builds one `ExperimentVersion` by hashing the three named config
    files (via the existing, unmodified `compute_file_hash`) plus the
    market-data provider/production designation, then deriving
    `version_id` from those hashes. Config paths default to this
    repository's real `config/*.yaml` files but are overridable so
    tests can point at temporary fixture files without ever touching
    the real ones (Step 1's own explicit testing requirement).

    This function performs no I/O beyond reading the three given config
    files as plain bytes (via `compute_file_hash`) -- it never opens a
    database, never calls a provider, never reads an environment
    variable."""
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")

    universe_config_hash = compute_file_hash(universe_config_path)
    risk_config_hash = compute_file_hash(risk_config_path)
    validation_config_hash = compute_file_hash(validation_config_path)
    market_data_config_hash = compute_market_data_config_hash(
        provider=market_data_provider, is_production=market_data_is_production,
    )
    version_id = compute_experiment_version_id(
        software_freeze_version=software_freeze_version,
        universe_config_hash=universe_config_hash,
        strategy_activation_stage=strategy_activation_stage,
        risk_config_hash=risk_config_hash,
        validation_config_hash=validation_config_hash,
        market_data_config_hash=market_data_config_hash,
    )
    return ExperimentVersion(
        version_id=version_id,
        software_freeze_version=software_freeze_version,
        universe_config_hash=universe_config_hash,
        strategy_activation_stage=strategy_activation_stage,
        risk_config_hash=risk_config_hash,
        validation_config_hash=validation_config_hash,
        market_data_config_hash=market_data_config_hash,
        recorded_at=recorded_at,
    )
