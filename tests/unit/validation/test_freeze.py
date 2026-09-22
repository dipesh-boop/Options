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
        assert manifest.freeze_name == "PAPER_TRADING_V1.4.1"
        assert manifest.freeze_version == "1.4.1"
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
