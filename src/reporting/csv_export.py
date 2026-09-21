"""Part 14: flat CSV exports -- one row per record, suitable for
independent spreadsheet/analysis tooling. Every writer takes the same
`ValidationReportBundle` every other format reads from."""
from __future__ import annotations

import csv
import math
from pathlib import Path

from src.reporting.data import ValidationReportBundle


def _write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("inf" if isinstance(v, float) and math.isinf(v) else v) for k, v in row.items()})


def export_csv(bundle: ValidationReportBundle, output_dir: Path | str) -> dict[str, Path]:
    """Writes the 9 named CSV files Part 14 lists, plus a manifest.csv
    describing the cohort itself. Returns a dict of {logical_name: path}."""
    out = Path(output_dir)
    written: dict[str, Path] = {}

    daily_path = out / "daily_portfolio.csv"
    _write_rows(daily_path, ["snapshot_date", "nav", "cash", "capital_deployed_pct", "open_position_count", "drawdown_pct", "recorded_at"], [
        {"snapshot_date": s.snapshot_date.isoformat(), "nav": s.nav, "cash": s.cash, "capital_deployed_pct": s.capital_deployed_pct,
         "open_position_count": s.open_position_count, "drawdown_pct": s.drawdown_pct, "recorded_at": s.recorded_at.isoformat()}
        for s in sorted(bundle.snapshots, key=lambda s: s.snapshot_date)
    ])
    written["daily_portfolio"] = daily_path

    trades_path = out / "trades.csv"
    _write_rows(trades_path, [
        "position_id", "ticker", "strategy", "contracts", "opened_at", "closed_at", "close_reason",
        "capital_at_risk", "realistic_entry_credit", "realistic_exit_debit", "theoretical_entry_credit",
        "theoretical_exit_debit", "commission_paid", "realistic_pnl", "theoretical_pnl", "entry_spread_pct",
    ], [
        {"position_id": t.position_id, "ticker": t.ticker, "strategy": t.strategy.value, "contracts": t.contracts,
         "opened_at": t.opened_at.isoformat(), "closed_at": t.closed_at.isoformat(), "close_reason": t.close_reason,
         "capital_at_risk": t.capital_at_risk, "realistic_entry_credit": t.realistic_entry_credit,
         "realistic_exit_debit": t.realistic_exit_debit, "theoretical_entry_credit": t.theoretical_entry_credit,
         "theoretical_exit_debit": t.theoretical_exit_debit, "commission_paid": t.commission_paid,
         "realistic_pnl": t.realistic_pnl, "theoretical_pnl": t.theoretical_pnl, "entry_spread_pct": t.entry_spread_pct}
        for t in sorted(bundle.trades, key=lambda t: t.opened_at)
    ])
    written["trades"] = trades_path

    strat_path = out / "strategy_performance.csv"
    _write_rows(strat_path, [
        "strategy", "trade_count", "win_count", "win_rate", "total_realistic_pnl", "total_theoretical_pnl",
        "total_commission_paid", "average_realistic_pnl", "expectancy", "largest_win", "largest_loss",
    ], [vars(r) for r in bundle.strategy_performance])
    written["strategy_performance"] = strat_path

    exec_path = out / "execution_quality.csv"
    _write_rows(exec_path, [
        "strategy", "trade_count", "average_entry_spread_pct", "average_realistic_vs_theoretical_entry_gap", "total_commission_paid",
    ], [vars(r) for r in bundle.execution_quality])
    written["execution_quality"] = exec_path

    risk_path = out / "risk_metrics.csv"
    rm = bundle.risk_metrics
    _write_rows(risk_path, list(vars(rm).keys()), [vars(rm)])
    written["risk_metrics"] = risk_path

    benchmarks_path = out / "benchmarks.csv"
    _write_rows(benchmarks_path, ["note"], [{"note": bundle.benchmark_comparison_note}])
    written["benchmarks"] = benchmarks_path

    counterfactuals_path = out / "counterfactuals.csv"
    _write_rows(counterfactuals_path, [
        "opportunity_id", "strategy_kind", "was_selected", "selection_or_rejection_reason",
        "expected_value", "maximum_loss", "probability_of_profit",
    ], [vars(r) for r in bundle.selection_records])
    written["counterfactuals"] = counterfactuals_path

    decision_path = out / "decision_quality.csv"
    _write_rows(decision_path, ["opportunity_id", "ticker", "pipeline_status", "risk_decision", "cash_no_trade", "alternative_count"],
                [vars(r) for r in bundle.decision_quality])
    written["decision_quality"] = decision_path

    compliance_path = out / "rule_compliance.csv"
    _write_rows(compliance_path, ["violation_id", "occurred_at", "rule_name", "description", "related_id"], [
        {"violation_id": v.violation_id, "occurred_at": v.occurred_at.isoformat(), "rule_name": v.rule_name,
         "description": v.description, "related_id": v.related_id}
        for v in bundle.violations
    ])
    written["rule_compliance"] = compliance_path

    return written
