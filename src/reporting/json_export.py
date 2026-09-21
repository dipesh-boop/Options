"""Part 13: a complete, machine-readable, versioned JSON export.
Uses the exact same generic `pydantic.TypeAdapter`-based serializer
`src.validation.records`/`SqliteValidationStore` already use for
persistence (see that module's own docstring on why this is a
generic, non-lossy mechanism) -- the export is never a second,
hand-written serialization that could drift from the storage layer's
own.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.validation.records import to_jsonable
from src.validation.session import DailySnapshot, ReconciliationFailureRecord, RuleViolationRecord

from src.reporting.data import (
    DecisionQualityRow,
    ExecutionQualityRow,
    RiskMetricsSummary,
    SelectionRecordRow,
    StrategyPerformanceRow,
    ValidationReportBundle,
)
from src.backtest.simulator import TradeRecord
from src.validation.protocol import StrategyVersionManifest
from src.validation.records import CohortRecord, OpportunityRecord


def export_json(bundle: ValidationReportBundle, output_path: Path | str) -> Path:
    payload = {
        "export_schema_version": bundle.export_schema_version,
        "generated_at": bundle.generated_at.isoformat(),
        "cohort": to_jsonable(bundle.cohort, CohortRecord) if bundle.cohort is not None else None,
        "portfolio_daily_history": [to_jsonable(s, DailySnapshot) for s in bundle.snapshots],
        "trades": [to_jsonable(t, TradeRecord) for t in bundle.trades],
        "opportunities": [to_jsonable(o, OpportunityRecord) for o in bundle.opportunities],
        "rule_compliance": [to_jsonable(v, RuleViolationRecord) for v in bundle.violations],
        "reconciliation_failures": [to_jsonable(f, ReconciliationFailureRecord) for f in bundle.reconciliation_failures],
        "strategy_performance": [to_jsonable(r, StrategyPerformanceRow) for r in bundle.strategy_performance],
        "risk_metrics": to_jsonable(bundle.risk_metrics, RiskMetricsSummary),
        "execution_quality": [to_jsonable(r, ExecutionQualityRow) for r in bundle.execution_quality],
        "decision_quality": [to_jsonable(r, DecisionQualityRow) for r in bundle.decision_quality],
        "counterfactual_strategy_selection": [to_jsonable(r, SelectionRecordRow) for r in bundle.selection_records],
        "benchmark_comparison": bundle.benchmark_comparison_note,
        "statistical_scorecard": bundle.statistical_scorecard_note,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
    return path
