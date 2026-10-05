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
        assert manifest.freeze_name == "PAPER_TRADING_V1.5.11"
        assert manifest.freeze_version == "1.5.11"
        assert manifest.required_options_feed_for_validation == "opra"
        assert len(manifest.alpaca_provider_module_hash) == 64
        assert len(manifest.wheel_module_hash) == 64
        assert manifest.wheel_strategy_kind_trade_proposal_eligible is False
        assert len(manifest.lifecycle_module_hash) == 64
        assert manifest.lifecycle_named_policy_count >= 19
        assert len(manifest.tradier_provider_module_hash) == 64
        assert len(manifest.rate_limiter_module_hash) == 64
        assert len(manifest.quality_gate_module_hash) == 64
        assert len(manifest.portfolio_module_hash) == 64
        assert manifest.tradier_market_data_only is True
        assert manifest.control_loop_cannot_execute_trades is True
        assert len(manifest.smoke_tradier_script_hash) == 64
        assert len(manifest.control_loop_projection_module_hash) == 64
        assert manifest.dashboard_cannot_execute_trades is True
        assert manifest.orchestrator_cannot_bypass_risk_or_lifecycle is True
        assert manifest.opportunity_scan_never_outranks_risk_monitoring is True
        assert len(manifest.review_module_hash) == 64
        assert len(manifest.run_validation_cycle_script_hash) == 64
        assert len(manifest.confirm_candidate_script_hash) == 64
        assert manifest.daily_cycle_never_calls_place_order is True
        assert manifest.review_only_path_never_imports_llm is True
        # Step 22.6 (PAPER_TRADING_V1.4.5)
        assert len(manifest.factory_module_hash) == 64
        assert manifest.help_cannot_execute_validation is True
        assert manifest.official_cycle_requires_tradier_preflight is True
        # Step 22.7 (PAPER_TRADING_V1.4.6)
        assert len(manifest.data_provider_module_hash) == 64
        # Step 22.8 (PAPER_TRADING_V1.4.7)
        assert len(manifest.dashboard_app_module_hash) == 64
        assert len(manifest.dashboard_validation_ops_module_hash) == 64
        assert manifest.dashboard_cannot_confirm_candidates is True
        assert manifest.fidelity_execution_mode == "MANUAL_EXECUTION"
        assert manifest.live_trading_enabled is False
        assert manifest.automatic_fidelity_execution is False
        assert manifest.validation_cohort_started is False
        # Step 23-1 (PAPER_TRADING_V1.5.0): experiment-version metadata foundation.
        assert len(manifest.experiment_version_module_hash) == 64
        assert len(manifest.validation_protocol_module_hash) == 64
        assert len(manifest.validation_session_module_hash) == 64
        # PAPER_TRADING_V1.5.1, Step 2
        assert manifest.market_hours_gate_precedes_mutation is True
        # PAPER_TRADING_V1.5.2, Step 2A: narrow frontend-only hotfix (explicit
        # window export for operator_control.js's helpers + software-version
        # badge bump) -- no new hashed module or safety-flag field, since
        # nothing under a hashed directory or Python safety check changed.
        # PAPER_TRADING_V1.5.3, Step 3: sector/correlation risk-data wiring.
        assert manifest.risk_data_wiring_fail_closed_verified is True
        assert manifest.risk_data_wiring_inactive_for_active_cohort is True
        # PAPER_TRADING_V1.5.4, Step 3B: Tradier historical daily-bars
        # adapter + date-intersection correlation-alignment correction.
        assert manifest.historical_data_capability_installed is True
        assert manifest.correlation_alignment_uses_date_intersection is True
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
        # Step 22.5: now real, hashed config -- was "not applicable" through V1.4.3.
        assert manifest.config_file_hashes["universe.yaml"] is not None
        assert manifest.config_file_hashes["operations.yaml"] is not None
        # Documented not-applicable entry, never silently omitted.
        assert manifest.config_file_hashes["strategies.yaml"] is None

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

    def test_tampering_with_the_alpaca_provider_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        alpaca_module = Path("src/data/alpaca_provider.py")
        original = alpaca_module.read_text(encoding="utf-8")
        try:
            alpaca_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "alpaca_provider_module_hash" and not c.passed for c in result.checks)
        finally:
            alpaca_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_wheel_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        wheel_module = Path("src/wheel/lifecycle.py")
        original = wheel_module.read_text(encoding="utf-8")
        try:
            wheel_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "wheel_module_hash" and not c.passed for c in result.checks)
        finally:
            wheel_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_lifecycle_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        lifecycle_module = Path("src/lifecycle/engine.py")
        original = lifecycle_module.read_text(encoding="utf-8")
        try:
            lifecycle_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "lifecycle_module_hash" and not c.passed for c in result.checks)
        finally:
            lifecycle_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_tradier_provider_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tradier_module = Path("src/data/tradier_provider.py")
        original = tradier_module.read_text(encoding="utf-8")
        try:
            tradier_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "tradier_provider_module_hash" and not c.passed for c in result.checks)
        finally:
            tradier_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_portfolio_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        portfolio_module = Path("src/portfolio/control_loop.py")
        original = portfolio_module.read_text(encoding="utf-8")
        try:
            portfolio_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "portfolio_module_hash" and not c.passed for c in result.checks)
        finally:
            portfolio_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_smoke_tradier_script_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/smoke_tradier_market_data.py")
        original = script.read_text(encoding="utf-8")
        try:
            script.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "smoke_tradier_script_hash" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_tampering_with_the_control_loop_projection_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        projection_module = Path("src/dashboard/control_loop_projection.py")
        original = projection_module.read_text(encoding="utf-8")
        try:
            projection_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "control_loop_projection_module_hash" and not c.passed for c in result.checks)
        finally:
            projection_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_review_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        review_module = Path("src/review/candidates.py")
        original = review_module.read_text(encoding="utf-8")
        try:
            review_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "review_module_hash" and not c.passed for c in result.checks)
        finally:
            review_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_run_validation_cycle_script_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            script.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "run_validation_cycle_script_hash" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_tampering_with_the_confirm_candidate_script_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/confirm_candidate.py")
        original = script.read_text(encoding="utf-8")
        try:
            script.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "confirm_candidate_script_hash" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_tampering_with_the_factory_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        factory_module = Path("src/data/factory.py")
        original = factory_module.read_text(encoding="utf-8")
        try:
            factory_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "factory_module_hash" and not c.passed for c in result.checks)
        finally:
            factory_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_data_provider_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        data_provider_module = Path("src/data/provider.py")
        original = data_provider_module.read_text(encoding="utf-8")
        try:
            data_provider_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "data_provider_module_hash" and not c.passed for c in result.checks)
        finally:
            data_provider_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_dashboard_app_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        dashboard_app_module = Path("src/dashboard/app.py")
        original = dashboard_app_module.read_text(encoding="utf-8")
        try:
            dashboard_app_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "dashboard_app_module_hash" and not c.passed for c in result.checks)
        finally:
            dashboard_app_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_dashboard_validation_ops_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        dashboard_validation_ops_module = Path("src/dashboard/validation_ops.py")
        original = dashboard_validation_ops_module.read_text(encoding="utf-8")
        try:
            dashboard_validation_ops_module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "dashboard_validation_ops_module_hash" and not c.passed for c in result.checks)
        finally:
            dashboard_validation_ops_module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_experiment_version_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        module = Path("src/validation/experiment_version.py")
        original = module.read_text(encoding="utf-8")
        try:
            module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "experiment_version_module_hash" and not c.passed for c in result.checks)
        finally:
            module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_validation_protocol_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        module = Path("src/validation/protocol.py")
        original = module.read_text(encoding="utf-8")
        try:
            module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "validation_protocol_module_hash" and not c.passed for c in result.checks)
        finally:
            module.write_text(original, encoding="utf-8")

    def test_tampering_with_the_validation_session_module_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        module = Path("src/validation/session.py")
        original = module.read_text(encoding="utf-8")
        try:
            module.write_text(original + "\n# drift test\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "validation_session_module_hash" and not c.passed for c in result.checks)
        finally:
            module.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_daily_cycle_never_calls_place_order_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            script.write_text(original + "\nplace_order()\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "daily_cycle_never_calls_place_order" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_review_only_path_never_imports_llm_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        confirmation_module = Path("src/review/confirmation.py")
        original = confirmation_module.read_text(encoding="utf-8")
        try:
            confirmation_module.write_text(original + "\nimport src.llm.client\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "review_only_path_never_imports_llm" and not c.passed for c in result.checks)
        finally:
            confirmation_module.write_text(original, encoding="utf-8")


class TestAlpacaMarketDataOnlyCheck:
    def test_alpaca_market_data_only_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "alpaca_market_data_only" and c.passed for c in result.checks)

    def test_alpaca_trading_import_added_to_src_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/data/_temp_drift_probe.py")
        try:
            poison_file.write_text("from alpaca.trading.client import TradingClient\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "alpaca_market_data_only" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)


class TestWheelSafetyChecks:
    def test_wheel_no_live_trading_client_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "wheel_no_live_trading_client" and c.passed for c in result.checks)

    def test_live_trading_client_import_added_to_wheel_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/wheel/_temp_drift_probe.py")
        try:
            poison_file.write_text("from alpaca.trading.client import TradingClient\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "wheel_no_live_trading_client" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_wheel_never_becomes_its_own_order_type_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "wheel_never_becomes_its_own_order_type" and c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_wheel_is_trade_proposal_eligible_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["wheel_strategy_kind_trade_proposal_eligible"] = True
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "wheel_never_becomes_its_own_order_type" and not c.passed for c in result.checks)


class TestLifecycleSafetyChecks:
    def test_lifecycle_no_live_trading_client_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "lifecycle_no_live_trading_client" and c.passed for c in result.checks)

    def test_live_trading_client_import_added_to_lifecycle_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/lifecycle/_temp_drift_probe.py")
        try:
            poison_file.write_text("from alpaca.trading.client import TradingClient\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "lifecycle_no_live_trading_client" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_lifecycle_named_policy_count_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "lifecycle_named_policy_count" and c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_too_few_lifecycle_policies_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["lifecycle_named_policy_count"] = 1
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "lifecycle_named_policy_count" and not c.passed for c in result.checks)


class TestTradierMarketDataOnlyCheck:
    def test_tradier_market_data_only_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "tradier_market_data_only" and c.passed for c in result.checks)

    def test_tradier_order_shaped_class_added_to_src_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/data/_temp_drift_probe.py")
        try:
            poison_file.write_text("class TradierBroker:\n    pass\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "tradier_market_data_only" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_a_manifest_falsely_claiming_tradier_market_data_only_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["tradier_market_data_only"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "tradier_market_data_only" and not c.passed for c in result.checks)


class TestPortfolioControlLoopSafetyChecks:
    def test_control_loop_cannot_execute_trades_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "control_loop_cannot_execute_trades" and c.passed for c in result.checks)

    def test_live_trading_client_import_added_to_portfolio_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/portfolio/_temp_drift_probe.py")
        try:
            poison_file.write_text("from alpaca.trading.client import TradingClient\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "control_loop_cannot_execute_trades" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_order_submission_method_call_added_to_portfolio_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/portfolio/_temp_drift_probe.py")
        try:
            poison_file.write_text("def f(client):\n    client.place_order(1)\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "control_loop_cannot_execute_trades" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_a_manifest_falsely_claiming_control_loop_cannot_execute_trades_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["control_loop_cannot_execute_trades"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "control_loop_cannot_execute_trades" and not c.passed for c in result.checks)


class TestDashboardCannotExecuteTradesCheck:
    def test_dashboard_cannot_execute_trades_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "dashboard_cannot_execute_trades" and c.passed for c in result.checks)

    def test_live_trading_client_import_added_to_dashboard_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/dashboard/_temp_drift_probe.py")
        try:
            poison_file.write_text("from alpaca.trading.client import TradingClient\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "dashboard_cannot_execute_trades" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_order_submission_method_call_added_to_dashboard_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/dashboard/_temp_drift_probe.py")
        try:
            poison_file.write_text("def f(client):\n    client.place_order(1)\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "dashboard_cannot_execute_trades" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_the_dashboards_own_legitimate_cancel_order_action_never_trips_this_check(self, tmp_path):
        # `src.dashboard.service.cancel_order` (Step 18's own pre-existing
        # CANCELLED action, pulling back an already-entered Fidelity
        # ticket via the existing `transition()` state machine) must
        # never be confused with a live broker order-cancellation call.
        manifest = build_freeze_manifest(generated_at=NOW)
        assert manifest.dashboard_cannot_execute_trades is True

    def test_a_manifest_falsely_claiming_dashboard_cannot_execute_trades_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["dashboard_cannot_execute_trades"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "dashboard_cannot_execute_trades" and not c.passed for c in result.checks)


class TestOrchestratorCannotBypassRiskOrLifecycleCheck:
    def test_orchestrator_cannot_bypass_risk_or_lifecycle_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "orchestrator_cannot_bypass_risk_or_lifecycle" and c.passed for c in result.checks)

    def test_a_direct_risk_engine_import_added_to_the_orchestrator_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        orchestrator_module = Path("src/portfolio/orchestrator.py")
        original = orchestrator_module.read_text(encoding="utf-8")
        try:
            orchestrator_module.write_text(original + "\nfrom src.risk.engine import evaluate_trade_proposal\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "orchestrator_cannot_bypass_risk_or_lifecycle" and not c.passed for c in result.checks)
        finally:
            orchestrator_module.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_orchestrator_does_not_bypass_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["orchestrator_cannot_bypass_risk_or_lifecycle"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "orchestrator_cannot_bypass_risk_or_lifecycle" and not c.passed for c in result.checks)


class TestOpportunityScanNeverOutranksRiskMonitoringCheck:
    def test_priority_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "opportunity_scan_never_outranks_risk_monitoring" and c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_the_priority_ordering_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["opportunity_scan_never_outranks_risk_monitoring"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "opportunity_scan_never_outranks_risk_monitoring" and not c.passed for c in result.checks)


class TestHelpCannotExecuteValidationCheck:
    """Step 22.6 (PAPER_TRADING_V1.4.5): `--help` (and any unrecognized
    argument) must never be able to reach a mutating call, because
    `argparse.ArgumentParser.parse_args()` runs strictly before either
    mutating entry point in `main()`."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "help_cannot_execute_validation" and c.passed for c in result.checks)

    def test_removing_argparse_construction_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            poisoned = original.replace("argparse.ArgumentParser", "_NotArgparseAnymore")
            assert poisoned != original  # sanity: the replacement actually did something
            script.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "help_cannot_execute_validation" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_calling_parse_args_after_the_mutating_entry_points_is_caught(self, tmp_path):
        # Simulates the actual regression this check exists to prevent:
        # main() reaching run_validation_cycle()/run_preflight() before
        # parser.parse_args() has had a chance to exit() on --help or an
        # unknown argument.
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            poisoned = original.replace(".parse_args(", ".parse_args_MOVED_LATER(")
            assert poisoned != original
            script.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "help_cannot_execute_validation" and not c.passed for c in result.checks)
        finally:
            script.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_help_cannot_execute_validation_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["help_cannot_execute_validation"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "help_cannot_execute_validation" and not c.passed for c in result.checks)


class TestOfficialCycleRequiresTradierPreflightCheck:
    """Step 22.6 (PAPER_TRADING_V1.4.5): the official, state-mutating
    validation cycle must call `verify_official_provider_is_tradier_
    production` strictly before its first mutation (candidate expiry,
    portfolio/account-state save, or daily snapshot record)."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "official_cycle_requires_tradier_preflight" and c.passed for c in result.checks)

    def test_removing_the_preflight_call_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            poisoned = original.replace(
                "verify_official_provider_is_tradier_production(",
                "_verify_official_provider_is_tradier_production_RENAMED(",
            )
            assert poisoned != original
            script.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "official_cycle_requires_tradier_preflight" and not c.passed for c in result.checks
            )
        finally:
            script.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_official_cycle_requires_tradier_preflight_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["official_cycle_requires_tradier_preflight"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "official_cycle_requires_tradier_preflight" and not c.passed for c in result.checks
        )


class TestMarketHoursGatePrecedesMutationCheck:
    """PAPER_TRADING_V1.5.1, Step 2: the new-position daily opportunity
    scan must never start outside the approved regular market session
    -- `scripts/run_validation_cycle.py` must call
    `evaluate_validation_cycle_eligibility` before every one of its own
    known mutating calls, exactly mirroring
    `TestOfficialCycleRequiresTradierPreflightCheck`'s own precedent."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "market_hours_gate_precedes_mutation" and c.passed for c in result.checks)

    def test_removing_the_gate_call_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        script = Path("scripts/run_validation_cycle.py")
        original = script.read_text(encoding="utf-8")
        try:
            poisoned = original.replace(
                "evaluate_validation_cycle_eligibility(",
                "_evaluate_validation_cycle_eligibility_RENAMED(",
            )
            assert poisoned != original
            script.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "market_hours_gate_precedes_mutation" and not c.passed for c in result.checks
            )
        finally:
            script.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_market_hours_gate_precedes_mutation_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["market_hours_gate_precedes_mutation"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "market_hours_gate_precedes_mutation" and not c.passed for c in result.checks
        )


class TestRiskDataWiringFailClosedCheck:
    """PAPER_TRADING_V1.5.3, Step 3: the sector/correlation
    fail-closed path must be demonstrably present, mirroring
    TestMarketHoursGatePrecedesMutationCheck's own precedent."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "risk_data_wiring_fail_closed_verified" and c.passed for c in result.checks)

    def test_removing_the_fail_closed_error_class_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        correlation_path = Path("src/risk/correlation.py")
        original = correlation_path.read_text(encoding="utf-8")
        try:
            poisoned = original.replace(
                "class CorrelationDataUnavailableError", "class _RenamedCorrelationDataUnavailableError"
            )
            assert poisoned != original
            correlation_path.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "risk_data_wiring_fail_closed_verified" and not c.passed for c in result.checks
            )
        finally:
            correlation_path.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_fail_closed_is_verified_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["risk_data_wiring_fail_closed_verified"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "risk_data_wiring_fail_closed_verified" and not c.passed for c in result.checks
        )


class TestRiskDataWiringInactiveForActiveCohortCheck:
    """PAPER_TRADING_V1.5.3, Step 3: installing this capability must
    never silently activate it for the currently active validation
    cohort -- `config/operations.yaml`'s `risk_data_wiring.enabled`
    must be `false` at freeze time."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(
            c.name == "risk_data_wiring_inactive_for_active_cohort" and c.passed for c in result.checks
        )

    def test_turning_it_on_in_the_real_config_is_caught(self, tmp_path, monkeypatch):
        """The dangerous scenario this check exists for: someone flips
        config/operations.yaml's risk_data_wiring.enabled to true,
        silently activating fail-closed behavior for the already-running
        cohort. `verify_freeze` must catch it, not just a hand-crafted
        JSON tamper."""
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        ops_yaml = Path("config/operations.yaml")
        original = ops_yaml.read_text(encoding="utf-8")
        try:
            poisoned = original.replace("enabled: false", "enabled: true", 1)
            assert poisoned != original
            ops_yaml.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "risk_data_wiring_inactive_for_active_cohort" and not c.passed for c in result.checks
            )
        finally:
            ops_yaml.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_inactive_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["risk_data_wiring_inactive_for_active_cohort"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "risk_data_wiring_inactive_for_active_cohort" and not c.passed for c in result.checks
        )


class TestDashboardCannotConfirmCandidatesCheck:
    """Step 22.8 (PAPER_TRADING_V1.4.7): no file under `src/dashboard/`
    may import `src.review.confirmation` or `confirm_candidate` --
    confirming a Review-Only candidate must always stay a separate,
    deliberate `scripts/confirm_candidate.py` operator command."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "dashboard_cannot_confirm_candidates" and c.passed for c in result.checks)

    def test_an_import_of_confirmation_added_to_the_dashboard_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        poison_file = Path("src/dashboard/_temp_drift_probe.py")
        try:
            poison_file.write_text("from src.review.confirmation import confirm_candidate\n", encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(c.name == "dashboard_cannot_confirm_candidates" and not c.passed for c in result.checks)
        finally:
            poison_file.unlink(missing_ok=True)

    def test_a_manifest_falsely_claiming_dashboard_cannot_confirm_candidates_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["dashboard_cannot_confirm_candidates"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "dashboard_cannot_confirm_candidates" and not c.passed for c in result.checks)


class TestHistoricalDataCapabilityInstalledCheck:
    """PAPER_TRADING_V1.5.4, Step 3B: TradierMarketDataProvider must
    demonstrably satisfy HistoricalDataProvider via a real get_bars
    method calling only /markets/history -- mirrors
    TestRiskDataWiringFailClosedCheck's own precedent."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "historical_data_capability_installed" and c.passed for c in result.checks)

    def test_removing_get_bars_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        provider_path = Path("src/data/tradier_provider.py")
        original = provider_path.read_text(encoding="utf-8")
        try:
            poisoned = original.replace("async def get_bars(", "async def _renamed_get_bars(")
            assert poisoned != original
            provider_path.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "historical_data_capability_installed" and not c.passed for c in result.checks
            )
        finally:
            provider_path.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_installed_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["historical_data_capability_installed"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "historical_data_capability_installed" and not c.passed for c in result.checks
        )


class TestCorrelationAlignmentUsesDateIntersectionCheck:
    """PAPER_TRADING_V1.5.4, Step 3B: this step's own explicitly-flagged
    critical correction to its own V1.5.3 code -- correlation histories
    must be aligned by true date intersection, never positional
    trimming. If the old `series[-aligned_length:]` pattern ever
    reappears, this check must catch it."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(
            c.name == "correlation_alignment_uses_date_intersection" and c.passed for c in result.checks
        )

    def test_reintroducing_the_old_positional_trim_bug_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        risk_data_path = Path("src/portfolio/risk_data.py")
        original = risk_data_path.read_text(encoding="utf-8")
        try:
            # Simulate the exact V1.5.3 regression this step fixed: a
            # positional-trim return statement reappearing in the module,
            # without removing the genuine intersection logic already
            # there -- the check must still fail on the mere PRESENCE of
            # the forbidden pattern.
            poisoned = original + "\n\n_REGRESSION_PROBE = lambda series, aligned_length: series[-aligned_length:]\n"
            assert poisoned != original
            risk_data_path.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "correlation_alignment_uses_date_intersection" and not c.passed for c in result.checks
            )
        finally:
            risk_data_path.write_text(original, encoding="utf-8")

    def test_removing_the_intersection_call_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        risk_data_path = Path("src/portfolio/risk_data.py")
        original = risk_data_path.read_text(encoding="utf-8")
        try:
            poisoned = original.replace("set.intersection(", "_renamed_intersection(")
            assert poisoned != original
            risk_data_path.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "correlation_alignment_uses_date_intersection" and not c.passed for c in result.checks
            )
        finally:
            risk_data_path.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_date_intersection_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["correlation_alignment_uses_date_intersection"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "correlation_alignment_uses_date_intersection" and not c.passed for c in result.checks
        )


class TestCandidateFunnelIsObservabilityOnlyCheck:
    """PAPER_TRADING_V1.5.5, Step 4: candidate-funnel diagnostics must be
    structurally incapable of influencing a trading decision -- every
    `FunnelDiagnostics.record_*` method returns `None`, and the
    `diagnostics`/`diagnostics_by_ticker` parameters default to `None`."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "candidate_funnel_is_observability_only" and c.passed for c in result.checks)

    def test_a_record_method_no_longer_returning_none_is_caught(self, tmp_path, monkeypatch):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        from src.workflows.funnel_diagnostics import FunnelDiagnostics

        def _record_chain_returns_something(self, *, contracts_seen: int, stale: bool) -> bool:
            self.chain_contracts_seen = contracts_seen
            self.chain_stale = stale
            return stale  # the regression this check must catch

        monkeypatch.setattr(FunnelDiagnostics, "record_chain", _record_chain_returns_something)
        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "candidate_funnel_is_observability_only" and not c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_observability_only_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["candidate_funnel_is_observability_only"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "candidate_funnel_is_observability_only" and not c.passed for c in result.checks)


class TestCandidateFunnelFieldIsOptionalAndAdditiveCheck:
    """PAPER_TRADING_V1.5.5, Step 4: `ControlCycleRecord.candidate_funnel`
    must default to `None`, so a pre-V1.5.5 record (lacking the key
    entirely) always deserializes cleanly, never requiring a backfill."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "candidate_funnel_field_is_optional_and_additive" and c.passed for c in result.checks)

    def test_a_manifest_falsely_claiming_optional_additive_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["candidate_funnel_field_is_optional_and_additive"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(
            c.name == "candidate_funnel_field_is_optional_and_additive" and not c.passed for c in result.checks
        )


class TestDashboardCandidateFunnelIsReadOnlyCheck:
    """PAPER_TRADING_V1.5.5, Step 4: no dashboard route may mutate or act
    on candidate-funnel data -- it stays a read-only projection."""

    def test_check_passes_on_the_real_repository(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")
        result = verify_freeze(path)
        assert any(c.name == "dashboard_candidate_funnel_is_read_only" and c.passed for c in result.checks)

    def test_a_mutating_route_referencing_candidate_funnel_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        app_path = Path("src/dashboard/app.py")
        original = app_path.read_text(encoding="utf-8")
        try:
            poisoned = original + (
                "\n\n@app.post('/api/_regression_probe')\n"
                "def _regression_probe():\n"
                "    return {'candidate_funnel': 'poisoned'}\n"
            )
            assert poisoned != original
            app_path.write_text(poisoned, encoding="utf-8")
            result = verify_freeze(path)
            assert result.passed is False
            assert any(
                c.name == "dashboard_candidate_funnel_is_read_only" and not c.passed for c in result.checks
            )
        finally:
            app_path.write_text(original, encoding="utf-8")

    def test_a_manifest_falsely_claiming_read_only_is_caught(self, tmp_path):
        manifest = build_freeze_manifest(generated_at=NOW)
        path = save_freeze_manifest(manifest, tmp_path / "manifest.json")

        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["dashboard_candidate_funnel_is_read_only"] = False
        from src.validation.freeze import compute_manifest_hash

        tampered["manifest_hash"] = compute_manifest_hash(tampered)
        path.write_text(json.dumps(tampered), encoding="utf-8")

        result = verify_freeze(path)
        assert result.passed is False
        assert any(c.name == "dashboard_candidate_funnel_is_read_only" and not c.passed for c in result.checks)
