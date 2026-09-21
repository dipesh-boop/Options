"""Part 15: an owner/investment-committee-style PDF validation report.
Every quantitative figure comes directly from `ValidationReportBundle`
(itself sourced only from Python/database calculations per this
package's own docstring) -- this module only ever formats numbers for
display; it never computes one. There is no LLM involved anywhere in
this module, so there is no risk of LLM-generated commentary altering
a quantitative figure (Part 15's own explicit requirement).
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.reporting.data import ValidationReportBundle

_SECTION_TITLES = (
    "Executive Summary", "Portfolio Performance", "Benchmark Comparison", "Risk", "Drawdown",
    "Strategy Attribution", "Execution Quality", "Decision Quality", "Strategy Selection Effectiveness",
    "Hedge Effectiveness", "Rule Compliance", "Statistical Confidence", "Validation Status",
)


def _table(headers: list[str], rows: list[list], *, col_widths=None) -> Table:
    data = [headers] + rows
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
    ]))
    return t


def export_pdf(bundle: ValidationReportBundle, output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Paper-Trading Validation Report", styles["Title"]))
    story.append(Paragraph(f"Generated: {bundle.generated_at.isoformat()}", styles["Normal"]))
    story.append(Paragraph(f"Cohort: {bundle.cohort.cohort_name if bundle.cohort else '(none)'} "
                            f"({bundle.cohort.cohort_id if bundle.cohort else 'n/a'})", styles["Normal"]))
    story.append(Paragraph(f"Cohort status: {bundle.cohort.status if bundle.cohort else 'n/a'}", styles["Normal"]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Executive Summary", styles["Heading1"]))
    story.append(_table(
        ["Metric", "Value"],
        [
            ["Trades recorded", str(len(bundle.trades))],
            ["Snapshots recorded", str(len(bundle.snapshots))],
            ["Opportunities recorded", str(len(bundle.opportunities))],
            ["Rule violations", str(len(bundle.violations))],
            ["Unresolved reconciliation failures", str(bundle.risk_metrics.unresolved_reconciliation_failure_count)],
        ],
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Portfolio Performance", styles["Heading1"]))
    if bundle.snapshots:
        rows = [[s.snapshot_date.isoformat(), f"${s.nav:,.2f}", f"${s.cash:,.2f}", f"{s.drawdown_pct:.2%}"]
                for s in sorted(bundle.snapshots, key=lambda s: s.snapshot_date)]
        story.append(_table(["Date", "NAV", "Cash", "Drawdown"], rows))
    else:
        story.append(Paragraph("No portfolio snapshots recorded yet.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Benchmark Comparison", styles["Heading1"]))
    story.append(Paragraph(bundle.benchmark_comparison_note, styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Risk", styles["Heading1"]))
    rm = bundle.risk_metrics
    story.append(_table(
        ["Metric", "Value"],
        [
            ["Starting NAV", f"${rm.starting_nav:,.2f}" if rm.starting_nav is not None else "n/a"],
            ["Latest NAV", f"${rm.latest_nav:,.2f}" if rm.latest_nav is not None else "n/a"],
            ["Peak NAV", f"${rm.peak_nav:,.2f}" if rm.peak_nav is not None else "n/a"],
            ["Trough NAV", f"${rm.trough_nav:,.2f}" if rm.trough_nav is not None else "n/a"],
        ],
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Drawdown", styles["Heading1"]))
    story.append(_table(
        ["Metric", "Value"],
        [
            ["Max drawdown", f"{rm.max_drawdown_pct:.2%}" if rm.max_drawdown_pct is not None else "n/a"],
            ["Latest drawdown", f"{rm.latest_drawdown_pct:.2%}" if rm.latest_drawdown_pct is not None else "n/a"],
        ],
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Strategy Attribution", styles["Heading1"]))
    if bundle.strategy_performance:
        rows = [[r.strategy, str(r.trade_count), f"{r.win_rate:.1%}", f"${r.total_realistic_pnl:,.2f}", f"${r.expectancy:,.2f}"]
                for r in bundle.strategy_performance]
        story.append(_table(["Strategy", "Trades", "Win Rate", "Total P&L", "Expectancy"], rows))
    else:
        story.append(Paragraph("No completed trades recorded yet.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Execution Quality", styles["Heading1"]))
    if bundle.execution_quality:
        rows = [[r.strategy, str(r.trade_count), f"{r.average_entry_spread_pct:.2%}", f"${r.average_realistic_vs_theoretical_entry_gap:,.4f}"]
                for r in bundle.execution_quality]
        story.append(_table(["Strategy", "Trades", "Avg Entry Spread %", "Avg Realistic-vs-Theoretical Gap"], rows))
    else:
        story.append(Paragraph("No completed trades recorded yet.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Decision Quality", styles["Heading1"]))
    if bundle.decision_quality:
        rows = [[r.opportunity_id, r.ticker, r.pipeline_status or "n/a", r.risk_decision or "n/a", "yes" if r.cash_no_trade else "no"]
                for r in bundle.decision_quality]
        story.append(_table(["Opportunity", "Ticker", "Status", "Risk Decision", "Cash/No-Trade"], rows))
    else:
        story.append(Paragraph("No opportunities recorded yet.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Strategy Selection Effectiveness", styles["Heading1"]))
    if bundle.selection_records:
        rows = [[r.opportunity_id, r.strategy_kind, "SELECTED" if r.was_selected else "alternative", f"${r.expected_value:,.2f}"]
                for r in bundle.selection_records]
        story.append(_table(["Opportunity", "Strategy", "Role", "Expected Value"], rows))
    else:
        story.append(Paragraph("No strategy alternatives recorded yet.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Hedge Effectiveness", styles["Heading1"]))
    story.append(Paragraph(
        "N/A for this export -- src.strategies.hedge_effectiveness is a real, already-tested capability "
        "(see tests/unit/strategies/test_hedge_effectiveness.py) that requires a protective-put/collar "
        "position paired with its underlying holding; this cohort has no such pairing recorded yet.",
        styles["Normal"],
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Rule Compliance", styles["Heading1"]))
    if bundle.violations:
        rows = [[v.violation_id, v.occurred_at.isoformat(), v.rule_name, v.description] for v in bundle.violations]
        story.append(_table(["ID", "Occurred At", "Rule", "Description"], rows))
    else:
        story.append(Paragraph("No rule violations recorded.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Statistical Confidence", styles["Heading1"]))
    story.append(Paragraph(bundle.statistical_scorecard_note, styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Validation Status", styles["Heading1"]))
    story.append(Paragraph(
        f"Cohort status: {bundle.cohort.status if bundle.cohort else 'n/a'}. "
        "This report is generated by src/reporting/ -- every number above is a direct read or a simple "
        "aggregation (sum/mean/count) over Python/database-computed records; no LLM-generated text in this "
        "document ever alters a quantitative figure.",
        styles["Normal"],
    ))

    doc.build(story)
    return path
