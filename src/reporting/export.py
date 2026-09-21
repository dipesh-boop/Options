"""The single entry point that produces every export format from one
`ValidationReportBundle` -- callers should use this rather than
calling the format-specific writers directly, so it's structurally
impossible to export CSV from one bundle and JSON from a different
(newer/older) one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.reporting.csv_export import export_csv
from src.reporting.data import ValidationReportBundle, build_report_bundle
from src.reporting.json_export import export_json
from src.reporting.pdf_export import export_pdf
from src.reporting.xlsx_export import export_xlsx
from src.validation.session import ValidationStore


@dataclass(frozen=True)
class ExportManifest:
    bundle: ValidationReportBundle
    csv_files: dict[str, Path]
    json_file: Path
    xlsx_file: Path
    pdf_file: Path


def export_validation_cohort(
    store: ValidationStore, *, cohort_id: str | None = None, output_dir: Path | str, generated_at: datetime,
) -> ExportManifest:
    """Builds one `ValidationReportBundle` from `store`, then writes
    every format from that exact same bundle into `output_dir`
    (`csv/`, `report.json`, `report.xlsx`, `report.pdf`)."""
    bundle = build_report_bundle(store, cohort_id=cohort_id, generated_at=generated_at)
    out = Path(output_dir)

    csv_files = export_csv(bundle, out / "csv")
    json_file = export_json(bundle, out / "report.json")
    xlsx_file = export_xlsx(bundle, out / "report.xlsx")
    pdf_file = export_pdf(bundle, out / "report.pdf")

    return ExportManifest(bundle=bundle, csv_files=csv_files, json_file=json_file, xlsx_file=xlsx_file, pdf_file=pdf_file)
