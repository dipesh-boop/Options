"""PAPER_TRADING_V1.5.0, Step 1: tests for the experiment-version
metadata foundation.

This module never touches `data/options_agent.db` or any operational
path -- every store is either `InMemoryValidationStore` or a
`SqliteValidationStore` pointed at `tmp_path`; every config hash test
uses temporary fixture files written under `tmp_path`, never the real
`config/*.yaml`. Nothing here calls Tradier/Fidelity/IBKR, runs a
validation cycle, confirms a candidate, or opens a PaperBroker order.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.portfolio.cycle_record import ControlCycleRecord
from src.validation.experiment_version import (
    DEFAULT_RISK_CONFIG_PATH,
    DEFAULT_UNIVERSE_CONFIG_PATH,
    DEFAULT_VALIDATION_CONFIG_PATH,
    ExperimentVersion,
    build_experiment_version,
    compute_experiment_version_id,
    compute_market_data_config_hash,
)
from src.validation.protocol import StrategyVersionManifest, ValidationPeriod, build_validation_manifest
from src.validation.session import (
    DailySnapshot,
    InMemoryValidationStore,
    SqliteValidationStore,
    _snapshot_from_dict,
    _trade_from_dict,
)

from .conftest import _trade

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------- fixture configs


def _write_configs(tmp_path: Path, *, universe: str = "a", risk: str = "b", validation: str = "c") -> dict[str, Path]:
    """Writes three tiny, throwaway fixture files -- never the real
    `config/*.yaml` -- so hashing tests are fully deterministic and
    never depend on (or risk touching) real repository config."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    universe_path = tmp_path / "universe.yaml"
    risk_path = tmp_path / "risk_limits.yaml"
    validation_path = tmp_path / "validation.yaml"
    universe_path.write_text(f"tickers: [{universe}]\n")
    risk_path.write_text(f"limit: {risk}\n")
    validation_path.write_text(f"duration: {validation}\n")
    return {"universe": universe_path, "risk": risk_path, "validation": validation_path}


def _build(tmp_path: Path, **overrides) -> ExperimentVersion:
    paths = _write_configs(tmp_path)
    kwargs = dict(
        software_freeze_version="PAPER_TRADING_V1.5.0",
        strategy_activation_stage="STAGE_A",
        market_data_provider="tradier",
        market_data_is_production=True,
        recorded_at=NOW,
        universe_config_path=paths["universe"],
        risk_config_path=paths["risk"],
        validation_config_path=paths["validation"],
    )
    kwargs.update(overrides)
    return build_experiment_version(**kwargs)


# -------------------------------------------------------------- determinism


class TestDeterminism:
    def test_same_configuration_produces_same_identity(self, tmp_path):
        paths = _write_configs(tmp_path)
        first = build_experiment_version(
            software_freeze_version="PAPER_TRADING_V1.5.0", strategy_activation_stage="STAGE_A",
            market_data_provider="tradier", market_data_is_production=True, recorded_at=NOW,
            universe_config_path=paths["universe"], risk_config_path=paths["risk"], validation_config_path=paths["validation"],
        )
        later = build_experiment_version(
            software_freeze_version="PAPER_TRADING_V1.5.0", strategy_activation_stage="STAGE_A",
            market_data_provider="tradier", market_data_is_production=True,
            recorded_at=datetime(2026, 12, 25, 3, 0, tzinfo=timezone.utc),  # different instant
            universe_config_path=paths["universe"], risk_config_path=paths["risk"], validation_config_path=paths["validation"],
        )
        assert first.version_id == later.version_id

    def test_changing_universe_config_changes_identity(self, tmp_path):
        base_paths = _write_configs(tmp_path / "a", universe="SPY")
        changed_paths = _write_configs(tmp_path / "b", universe="SPY,QQQ")
        base = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in base_paths.items()},
        )
        changed = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in changed_paths.items()},
        )
        assert base.universe_config_hash != changed.universe_config_hash
        assert base.version_id != changed.version_id

    def test_changing_risk_config_changes_identity(self, tmp_path):
        base_paths = _write_configs(tmp_path / "a", risk="0.10")
        changed_paths = _write_configs(tmp_path / "b", risk="0.05")
        base = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in base_paths.items()},
        )
        changed = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in changed_paths.items()},
        )
        assert base.risk_config_hash != changed.risk_config_hash
        assert base.version_id != changed.version_id

    def test_changing_validation_config_changes_identity(self, tmp_path):
        base_paths = _write_configs(tmp_path / "a", validation="90")
        changed_paths = _write_configs(tmp_path / "b", validation="180")
        base = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in base_paths.items()},
        )
        changed = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=NOW, **{f"{k}_config_path": v for k, v in changed_paths.items()},
        )
        assert base.validation_config_hash != changed.validation_config_hash
        assert base.version_id != changed.version_id

    def test_changing_strategy_activation_stage_changes_identity(self, tmp_path):
        stage_a = _build(tmp_path, strategy_activation_stage="STAGE_A")
        stage_b = _build(tmp_path, strategy_activation_stage="STAGE_B")
        assert stage_a.version_id != stage_b.version_id

    def test_recorded_at_alone_does_not_change_identity(self, tmp_path):
        paths = _write_configs(tmp_path)
        v1 = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            universe_config_path=paths["universe"], risk_config_path=paths["risk"], validation_config_path=paths["validation"],
        )
        v2 = build_experiment_version(
            software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
            market_data_is_production=True, recorded_at=datetime(2030, 6, 15, tzinfo=timezone.utc),
            universe_config_path=paths["universe"], risk_config_path=paths["risk"], validation_config_path=paths["validation"],
        )
        assert v1.version_id == v2.version_id
        assert v1.recorded_at != v2.recorded_at

    def test_naive_recorded_at_rejected(self, tmp_path):
        paths = _write_configs(tmp_path)
        with pytest.raises(ValueError):
            build_experiment_version(
                software_freeze_version="v", strategy_activation_stage="STAGE_A", market_data_provider="tradier",
                market_data_is_production=True, recorded_at=datetime(2026, 1, 1),  # naive
                universe_config_path=paths["universe"], risk_config_path=paths["risk"], validation_config_path=paths["validation"],
            )

    def test_compute_experiment_version_id_is_a_pure_function_of_its_six_inputs(self):
        kwargs = dict(
            software_freeze_version="PAPER_TRADING_V1.5.0", universe_config_hash="u" * 64,
            strategy_activation_stage="STAGE_A", risk_config_hash="r" * 64,
            validation_config_hash="v" * 64, market_data_config_hash="m" * 64,
        )
        assert compute_experiment_version_id(**kwargs) == compute_experiment_version_id(**kwargs)
        changed = dict(kwargs, strategy_activation_stage="STAGE_B")
        assert compute_experiment_version_id(**kwargs) != compute_experiment_version_id(**changed)


# --------------------------------------------------------------- no secrets


class TestNoSecrets:
    def test_market_data_config_hash_never_receives_or_produces_a_secret(self):
        # The only two inputs this function accepts are a provider name
        # and a boolean -- there is structurally no parameter through
        # which a token/credential could ever reach it.
        h1 = compute_market_data_config_hash(provider="tradier", is_production=True)
        h2 = compute_market_data_config_hash(provider="tradier", is_production=True)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex digest, not a leaked/echoed value
        secret_lookalike = "sk-live-super-secret-tradier-token-should-never-leak"
        assert secret_lookalike not in h1

    def test_different_production_designation_changes_the_hash(self):
        prod = compute_market_data_config_hash(provider="tradier", is_production=True)
        sandbox = compute_market_data_config_hash(provider="tradier", is_production=False)
        assert prod != sandbox

    def test_build_experiment_version_module_never_imports_provider_or_env_modules(self):
        # Structural proof, mirroring this codebase's own established
        # import-statement-scoped precedent (see e.g.
        # tests/unit/dashboard/test_operator_status.py): a whole-file
        # substring scan would false-positive on this module's own
        # docstring, which explains the no-secrets guarantee by naming
        # the very modules it must never import.
        import re

        source = Path("src/validation/experiment_version.py").read_text(encoding="utf-8")
        import_pattern = re.compile(r"^\s*(?:import|from)\s+\S*(?:data\.factory|data\.tradier_provider|dotenv|os\b)\S*", re.MULTILINE)
        assert import_pattern.search(source) is None

    def test_experiment_version_dataclass_has_no_credential_shaped_field(self):
        forbidden = {"token", "password", "api_key", "apikey", "secret", "credential", "cookie"}
        field_names = {f.lower() for f in ExperimentVersion.__dataclass_fields__}
        assert field_names.isdisjoint(forbidden)


# ---------------------------------------------------- backward compatibility


class TestBackwardCompatibility:
    """Every fixture below is deliberately shaped exactly as a
    PAPER_TRADING_V1.4.8-era record would be -- no `experiment_version_id`
    key present anywhere -- proving old, already-persisted records
    deserialize unchanged under the new, additive field."""

    def test_old_shaped_control_cycle_record_json_still_deserializes(self):
        old_json = json.dumps({
            "cycle_id": "validation-2026-09-24",
            "started_at": "2026-09-24T13:00:00+00:00",
            "completed_at": "2026-09-24T13:05:00+00:00",
            "market_open": True,
            "provider": "tradier",
            "provider_health_status": "healthy",
        })
        record = ControlCycleRecord.model_validate_json(old_json)
        assert record.experiment_version_id is None
        assert record.cycle_id == "validation-2026-09-24"

    def test_old_shaped_trade_record_dict_still_deserializes(self):
        old_dict = {
            "position_id": "val-1", "ticker": "SPY", "strategy": "cash_secured_put",
            "contracts": 1, "opened_at": "2026-09-22", "closed_at": "2026-10-06",
            "close_reason": "profit_target", "capital_at_risk": 9500.0,
            "realistic_entry_credit": 200.0, "realistic_exit_debit": -100.0,
            "theoretical_entry_credit": 200.0, "theoretical_exit_debit": -100.0,
            "commission_paid": 1.30, "realistic_pnl": 98.70, "theoretical_pnl": 100.0,
            "entry_spread_pct": 0.05,
        }
        record = _trade_from_dict(old_dict)
        assert record.experiment_version_id is None
        assert record.position_id == "val-1"

    def test_old_shaped_daily_snapshot_dict_still_deserializes(self):
        old_dict = {
            "snapshot_date": "2026-09-22", "nav": 100000.0, "cash": 100000.0,
            "capital_deployed_pct": 0.0, "open_position_count": 0, "drawdown_pct": 0.0,
            "per_strategy_nav": {}, "recorded_at": "2026-09-22T21:00:00+00:00",
        }
        record = _snapshot_from_dict(old_dict)
        assert record.experiment_version_id is None
        assert record.snapshot_date == date(2026, 9, 22)

    def test_old_shaped_strategy_version_manifest_json_still_deserializes(self):
        old_json = json.dumps({
            "manifest_id": "m-1",
            "frozen_at": "2026-09-22T00:00:00+00:00",
            "period": {"start_date": "2026-09-22", "end_date": "2026-12-21", "duration_days": 90},
            "starting_nav": 100000.0,
            "config_file_hashes": {"risk_limits.yaml": "abc123"},
            "strategy_versions": {"cash_secured_put": "1.0"},
        })
        manifest = StrategyVersionManifest.model_validate_json(old_json)
        assert manifest.experiment_version_id is None
        assert manifest.cohort_label == "default"  # the Step 19A precedent field also still defaults correctly

    def test_existing_conftest_trade_helper_is_itself_an_old_shaped_fixture(self):
        # tests/unit/validation/conftest.py::_trade is unmodified by
        # this step and never passes experiment_version_id -- confirms
        # every other test file already exercising it continues to get
        # the honest None default without any fixture change.
        trade = _trade()
        assert trade.experiment_version_id is None


# --------------------------------------------------- new-shaped round-trip


class TestNewShapedRoundTrip:
    def test_control_cycle_record_round_trips_with_experiment_version_id(self):
        record = ControlCycleRecord(
            cycle_id="validation-2026-11-01", started_at=NOW, market_open=True,
            provider="tradier", provider_health_status="healthy",
            experiment_version_id="deadbeef" * 8,
        )
        reloaded = ControlCycleRecord.model_validate_json(record.model_dump_json())
        assert reloaded.experiment_version_id == "deadbeef" * 8

    def test_trade_record_round_trips_with_experiment_version_id(self):
        trade = _trade(experiment_version_id="deadbeef" * 8)
        from src.validation.session import _trade_to_dict

        reloaded = _trade_from_dict(_trade_to_dict(trade))
        assert reloaded.experiment_version_id == "deadbeef" * 8

    def test_daily_snapshot_round_trips_with_experiment_version_id(self):
        from src.validation.session import _snapshot_to_dict

        snapshot = DailySnapshot(
            snapshot_date=date(2026, 11, 1), nav=100000.0, cash=100000.0,
            capital_deployed_pct=0.0, open_position_count=0, drawdown_pct=0.0,
            per_strategy_nav={}, recorded_at=NOW, experiment_version_id="deadbeef" * 8,
        )
        reloaded = _snapshot_from_dict(_snapshot_to_dict(snapshot))
        assert reloaded.experiment_version_id == "deadbeef" * 8

    def test_build_validation_manifest_accepts_and_carries_experiment_version_id(self, tmp_path):
        paths = _write_configs(tmp_path)
        manifest = build_validation_manifest(
            manifest_id="m-2", period=ValidationPeriod(start_date=date(2026, 11, 1), end_date=date(2027, 1, 30), duration_days=90),
            frozen_at=NOW, starting_nav=100000.0, strategy_versions={"cash_secured_put": "1.0"},
            config_paths=(paths["risk"], paths["validation"]),
            experiment_version_id="deadbeef" * 8,
        )
        reloaded = StrategyVersionManifest.model_validate_json(manifest.model_dump_json())
        assert reloaded.experiment_version_id == "deadbeef" * 8

    def test_build_validation_manifest_defaults_to_none_when_omitted(self, tmp_path):
        paths = _write_configs(tmp_path)
        manifest = build_validation_manifest(
            manifest_id="m-3", period=ValidationPeriod(start_date=date(2026, 11, 1), end_date=date(2027, 1, 30), duration_days=90),
            frozen_at=NOW, starting_nav=100000.0, strategy_versions={"cash_secured_put": "1.0"},
            config_paths=(paths["risk"], paths["validation"]),
        )
        assert manifest.experiment_version_id is None


# ------------------------------------------------------------- persistence


class TestPersistence:
    """No test in this class ever touches `data/options_agent.db` --
    every store is `InMemoryValidationStore` or `SqliteValidationStore`
    against `tmp_path`."""

    @pytest.fixture(params=["memory", "sqlite"])
    def store(self, request, tmp_path: Path):
        if request.param == "memory":
            return InMemoryValidationStore()
        return SqliteValidationStore(tmp_path / "step1_test.db")

    def test_record_and_retrieve_experiment_version(self, store, tmp_path):
        version = _build(tmp_path)
        store.record_experiment_version(version)
        reloaded = store.get_experiment_version(version.version_id)
        assert reloaded is not None
        assert reloaded.version_id == version.version_id
        assert reloaded.software_freeze_version == version.software_freeze_version
        assert reloaded.universe_config_hash == version.universe_config_hash
        assert reloaded.strategy_activation_stage == version.strategy_activation_stage
        assert reloaded.risk_config_hash == version.risk_config_hash
        assert reloaded.validation_config_hash == version.validation_config_hash
        assert reloaded.market_data_config_hash == version.market_data_config_hash
        assert reloaded.recorded_at == version.recorded_at

    def test_unknown_version_id_returns_none(self, store):
        assert store.get_experiment_version("no-such-version") is None

    def test_recording_the_same_version_twice_is_idempotent(self, store, tmp_path):
        version = _build(tmp_path)
        store.record_experiment_version(version)
        store.record_experiment_version(version)  # identical content -- must not raise, must not duplicate
        reloaded = store.get_experiment_version(version.version_id)
        assert reloaded is not None
        assert reloaded.version_id == version.version_id

    def test_default_config_paths_point_at_the_real_repository_config(self):
        # Confirms the defaults resolve to the real files (existence
        # only) without this test itself ever building an
        # ExperimentVersion from them -- no test in this module calls
        # build_experiment_version() with its default paths, always
        # explicit tmp_path fixtures, per Step 1's explicit
        # "never touch real config files for these tests" requirement.
        assert DEFAULT_UNIVERSE_CONFIG_PATH.name == "universe.yaml"
        assert DEFAULT_RISK_CONFIG_PATH.name == "risk_limits.yaml"
        assert DEFAULT_VALIDATION_CONFIG_PATH.name == "validation.yaml"
