"""Step 22 Parts 20-24: VALIDATION_MANIFEST.json build/save/load/verify
round-trip, drift detection, and the explicit safety-flag checks
`verify_freeze` must never let pass silently."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.validation.freeze import (
    FreezeManifestError,
    build_freeze_manifest,
    load_freeze_manifest,
    save_freeze_manifest,
    verify_freeze,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class TestBuildFreezeManifest:
    def test_builds_successfully_against_the_real_repository(self):
        manifest = build_freeze_manifest(generated_at=NOW)
        assert manifest.freeze_name == "PAPER_TRADING_V1.0"
        assert manifest.fidelity_execution_mode == "MANUAL_EXECUTION"
        assert manifest.live_trading_enabled is False
        assert manifest.automatic_fidelity_execution is False
        assert manifest.validation_cohort_started is False
        assert len(manifest.manifest_hash) == 64  # sha256 hex digest

    def test_naive_generated_at_rejected(self):
        with pytest.raises(FreezeManifestError):
            build_freeze_manifest(generated_at=datetime(2026, 9, 21, 12, 0))

    def test_no_secrets_or_api_keys_field_anywhere(self):
        manifest = build_freeze_manifest(generated_at=NOW)
        payload = json.loads(manifest.model_dump_json())
        blob = json.dumps(payload).lower()
        for forbidden in ("api_key", "password", "secret", "token", "credential"):
            assert forbidden not in blob, f"manifest unexpectedly contains {forbidden!r}"

    def test_config_hashes_present_for_every_named_file_and_na_for_the_rest(self):
        manifest = build_freeze_manifest(generated_at=NOW)
        assert manifest.config_file_hashes["risk_limits.yaml"] is not None
        assert manifest.config_file_hashes["brokers.yaml"] is not None
        assert manifest.config_file_hashes["validation.yaml"] is not None
        assert manifest.config_file_hashes["llm.yaml"] is not None
        # Documented not-applicable entries, never silently omitted.
        assert manifest.config_file_hashes["strategies.yaml"] is None
        assert manifest.config_file_hashes["universe.yaml"] is None

    def test_approved_strategies_reflect_actual_brokers_yaml(self):
        manifest = build_freeze_manifest(generated_at=NOW)
        assert "fidelity" in manifest.approved_strategies_by_broker
        assert "CASH_SECURED_PUT" in manifest.approved_strategies_by_broker["fidelity"]


class TestSaveLoadRoundTrip:
    def test_save_then_load_reconstructs_identical_manifest(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        reloaded = load_freeze_manifest(path)
        assert reloaded == manifest

    def test_load_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(FreezeManifestError):
            load_freeze_manifest(tmp_path / "does_not_exist.json")

    def test_load_malformed_json_fails_closed(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(FreezeManifestError):
            load_freeze_manifest(bad)


class TestVerifyFreezeCleanState:
    def test_a_freshly_built_and_saved_manifest_verifies_clean(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert result.passed is True
        assert result.failures == ()

    def test_missing_manifest_file_fails_closed(self, tmp_path):
        result = verify_freeze(tmp_path / "nope.json")
        assert result.passed is False
        assert any(c.name == "manifest_exists" for c in result.failures)


class TestVerifyFreezeDetectsDrift:
    """The core Part 22/24 guarantee: a material change to a frozen file
    must be caught, never silently absorbed into a passing verification."""

    def test_tampering_with_a_hashed_config_file_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        risk_limits = Path("config/risk_limits.yaml")
        original = risk_limits.read_text(encoding="utf-8")
        try:
            risk_limits.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "config_hash:risk_limits.yaml" and not c.passed for c in result.checks)
        finally:
            risk_limits.write_text(original, encoding="utf-8")

    def test_tampering_with_manifest_hash_itself_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["manifest_hash"] = "0" * 64
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "manifest_hash_self_consistent" and not c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_live_trading_enabled_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["live_trading_enabled"] = True
        # Re-point the hash so this isn't just caught by the self-consistency
        # check -- this proves `live_trading_disabled` fires independently.
        from src.validation.freeze import compute_manifest_hash
        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "live_trading_disabled" and not c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_validation_already_started_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["validation_cohort_started"] = True
        from src.validation.freeze import compute_manifest_hash
        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "validation_cohort_not_started" and not c.passed for c in result.checks)
