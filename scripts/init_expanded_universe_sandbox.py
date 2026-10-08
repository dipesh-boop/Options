#!/usr/bin/env python3
"""PAPER_TRADING_V1.5.15: initializer for the Expanded-Universe
Sandbox -- a strictly isolated, parallel research environment that
exercises the full paper-trading lifecycle under a WIDER 12-symbol
universe than the official SPY/QQQ cohort, using the exact same Quant/
Risk/DTE/liquidity/sizing/ranking/no-trade standards.

**This is NOT the official 90-day validation cohort.** It does not
touch, read, or write `data/options_agent.db`, the official cohort
`paper-trading-v1.4.3-validation-2026-09-22`, `config/universe.yaml`,
or any other official state -- see `src.portfolio.sandbox_guard`
(the official-database rejection gate, called BEFORE any store is
constructed) and `src.portfolio.sandbox_identity` (the sandbox's own
hardwired identity).

**Metadata/state setup only -- never contacts Tradier.** Creates the
sandbox database's schemas (via the existing, unmodified
`SqliteValidationStore`/`SqlitePortfolioStore`/
`SqlitePaperAccountStateStore`), a real, persisted
`ExperimentVersion`, and an active `CohortRecord` wrapping a
`StrategyVersionManifest` -- all pure metadata/state operations with
no network call of any kind. Provider preflight belongs to
`scripts/run_sandbox_cycle.py`, never here.

**Idempotent.** Running this twice is always safe:
- if the sandbox cohort does not exist yet, it is created fresh (NAV
  $100,000, zero positions, zero completed trades);
- if it already exists with the EXACT SAME identity (cohort name,
  label, manifest id, start date, starting NAV), this prints that fact
  and exits 0 without writing anything;
- if it already exists with a DIFFERENT identity (e.g. a manifest id
  or label from an incompatible prior run), this fails closed with an
  explanation and writes nothing -- it NEVER resets, overwrites, or
  silently keeps a mismatched record without telling the operator.

Usage:
    python scripts/init_expanded_universe_sandbox.py

Exit codes:
    0 = sandbox is initialized (freshly, or already correctly)
    1 = could not initialize (safety violation, config error, or an
        incompatible sandbox already exists)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.universe import UniverseConfigError, load_universe, load_universe_strategies  # noqa: E402
from src.portfolio.account_state import SqlitePortfolioStore  # noqa: E402
from src.portfolio.sandbox_guard import SandboxGuardError, assert_path_is_not_official_database  # noqa: E402
from src.portfolio.sandbox_identity import (  # noqa: E402
    SANDBOX_ACCOUNT_ID,
    SANDBOX_COHORT_ID,
    SANDBOX_COHORT_LABEL,
    SANDBOX_COHORT_NAME,
    SANDBOX_DATABASE_PATH,
    SANDBOX_DURATION_DAYS,
    SANDBOX_MANIFEST_ID,
    SANDBOX_SOFTWARE_FREEZE_VERSION,
    SANDBOX_START_DATE,
    SANDBOX_STARTING_NAV,
    SANDBOX_UNIVERSE_CONFIG_PATH,
    load_sandbox_operations_config,
)
from src.risk.portfolio_risk import Portfolio  # noqa: E402
from src.validation.experiment_version import build_experiment_version  # noqa: E402
from src.validation.protocol import DEFAULT_MANIFEST_CONFIG_PATHS, ValidationPeriod, build_validation_manifest  # noqa: E402
from src.validation.records import CohortRecord  # noqa: E402
from src.validation.session import SqliteValidationStore  # noqa: E402

_BANNER = (
    "EXPANDED-UNIVERSE SANDBOX -- INITIALIZER\n"
    "NOT OFFICIAL VALIDATION\n"
    "METADATA/STATE SETUP ONLY -- NO TRADIER CALL, NO MARKET DATA\n"
    "PAPERBROKER SIMULATION ONLY -- NO LIVE EXECUTION"
)

_STRATEGY_VERSIONS = {"cash_secured_put": "v1", "covered_call": "v1", "put_credit_spread": "v1"}


def _line(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def main() -> int:
    print(_BANNER)
    print()

    # Section 1/27A: the official-database rejection gate -- the FIRST
    # thing this script does, before any config is even loaded, before
    # any store is constructed, before any schema is initialized.
    try:
        assert_path_is_not_official_database(SANDBOX_DATABASE_PATH)
    except SandboxGuardError as exc:
        print(f"FAIL: {exc}")
        return 1

    try:
        ops = load_sandbox_operations_config()
        universe = load_universe(SANDBOX_UNIVERSE_CONFIG_PATH)
        load_universe_strategies(SANDBOX_UNIVERSE_CONFIG_PATH)  # validated, not otherwise needed here
    except UniverseConfigError as exc:
        print(f"FAIL: sandbox universe config error -- {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not load sandbox configuration -- {exc!r}")
        return 1

    # Defense in depth -- re-check every resolved sandbox storage path,
    # not just the hardwired constant, before touching any of them.
    try:
        for path in (
            ops.account_state_db_path, ops.control_loop_db_path,
            ops.lifecycle_db_path, ops.candidate_review_db_path,
        ):
            assert_path_is_not_official_database(path)
    except SandboxGuardError as exc:
        print(f"FAIL: {exc}")
        return 1

    _line("sandbox database", ops.account_state_db_path)
    _line("sandbox cohort_id", SANDBOX_COHORT_ID)
    _line("sandbox account_id", SANDBOX_ACCOUNT_ID)
    _line("sandbox universe config", str(SANDBOX_UNIVERSE_CONFIG_PATH))
    _line("sandbox universe symbols", [e.ticker for e in universe])

    now = datetime.now(timezone.utc)
    validation_store = SqliteValidationStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)

    period = ValidationPeriod(
        start_date=SANDBOX_START_DATE, end_date=SANDBOX_START_DATE + timedelta(days=SANDBOX_DURATION_DAYS),
        duration_days=SANDBOX_DURATION_DAYS,
    )

    existing_cohort = validation_store.get_cohort(SANDBOX_COHORT_ID)
    if existing_cohort is not None:
        manifest = existing_cohort.manifest
        matches = (
            existing_cohort.cohort_name == SANDBOX_COHORT_NAME
            and manifest.cohort_label == SANDBOX_COHORT_LABEL
            and manifest.manifest_id == SANDBOX_MANIFEST_ID
            and manifest.period.start_date == period.start_date
            and manifest.starting_nav == SANDBOX_STARTING_NAV
        )
        if not matches:
            print(
                f"FAIL: an INCOMPATIBLE sandbox cohort {SANDBOX_COHORT_ID!r} already exists in "
                f"{ops.account_state_db_path!r} -- refusing to overwrite it.\n"
                f"  existing: cohort_name={existing_cohort.cohort_name!r}, "
                f"manifest_id={manifest.manifest_id!r}, cohort_label={manifest.cohort_label!r}, "
                f"start_date={manifest.period.start_date!r}, starting_nav={manifest.starting_nav!r}\n"
                f"  expected: cohort_name={SANDBOX_COHORT_NAME!r}, manifest_id={SANDBOX_MANIFEST_ID!r}, "
                f"cohort_label={SANDBOX_COHORT_LABEL!r}, start_date={period.start_date!r}, "
                f"starting_nav={SANDBOX_STARTING_NAV!r}\n"
                "This sandbox is never reset automatically -- resolve the mismatch by hand before "
                "re-running this initializer."
            )
            return 1
        print(f"\nALREADY INITIALIZED: sandbox cohort {SANDBOX_COHORT_ID!r} already exists with the expected "
              "identity -- nothing written.")
        existing_portfolio = portfolio_store.get(SANDBOX_ACCOUNT_ID)
        if existing_portfolio is not None:
            _line("current NAV / cash / open positions", f"${existing_portfolio.nav:,.2f} / ${existing_portfolio.cash:,.2f} / {len(existing_portfolio.positions)}")
        return 0

    # Fresh initialization -- never reached on a second, matching run.
    experiment_version = build_experiment_version(
        software_freeze_version=SANDBOX_SOFTWARE_FREEZE_VERSION,
        strategy_activation_stage=SANDBOX_COHORT_LABEL,
        market_data_provider="tradier",
        market_data_is_production=True,
        recorded_at=now,
        universe_config_path=SANDBOX_UNIVERSE_CONFIG_PATH,
        risk_data_wiring_enabled=ops.risk_data_wiring_enabled,
    )
    validation_store.record_experiment_version(experiment_version)
    _line("experiment_version_id", experiment_version.version_id)
    _line("risk_data_wiring_enabled (recorded honestly, not guessed)", experiment_version.risk_data_wiring_enabled)

    manifest = build_validation_manifest(
        manifest_id=SANDBOX_MANIFEST_ID, period=period, frozen_at=now, starting_nav=SANDBOX_STARTING_NAV,
        strategy_versions=_STRATEGY_VERSIONS, config_paths=DEFAULT_MANIFEST_CONFIG_PATHS,
        notes=(
            "Experimental parallel sandbox. Not official validation. Expanded universe only. "
            "Same risk/Quant/liquidity/DTE/no-trade standards. Tradier production market data only. "
            "PaperBroker simulated execution only. Human confirmation required. "
            "No live trading authorization."
        ),
        cohort_label=SANDBOX_COHORT_LABEL, experiment_version_id=experiment_version.version_id,
    )
    cohort_record = CohortRecord(
        cohort_id=SANDBOX_COHORT_ID, cohort_name=SANDBOX_COHORT_NAME, status="created",
        created_at=now, manifest=manifest, started_at=None,
    )
    validation_store.record_cohort(cohort_record)
    _line("manifest_id", manifest.manifest_id)
    _line("cohort status", cohort_record.status)

    if portfolio_store.get(SANDBOX_ACCOUNT_ID) is None:
        portfolio = Portfolio(as_of=now, nav=SANDBOX_STARTING_NAV, cash=SANDBOX_STARTING_NAV, peak_equity=SANDBOX_STARTING_NAV)
        portfolio_store.save(SANDBOX_ACCOUNT_ID, portfolio)
        _line("portfolio bootstrapped", f"NAV ${SANDBOX_STARTING_NAV:,.2f}, cash ${SANDBOX_STARTING_NAV:,.2f}, 0 positions")
    else:
        _line("portfolio", "already present -- left untouched")

    print(
        "\nPASS: Expanded-Universe Sandbox initialized. No Tradier call was made. The official "
        "validation database was never touched."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
