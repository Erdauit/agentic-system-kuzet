"""Чтение отчёта `qoima doctor --report` из каталога данных (мастер, шаг 2; health «Время»)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from shared.schemas import DoctorReport

REPORT_RELATIVE = Path("doctor") / "report.json"


def load_report(data_root: Path) -> DoctorReport | None:
    path = data_root / REPORT_RELATIVE
    if not path.is_file():
        return None
    try:
        return DoctorReport.model_validate_json(path.read_bytes())
    except (ValidationError, OSError):
        return None


def report_age_minutes(report: DoctorReport) -> float:
    return (datetime.now(UTC) - report.generated_at).total_seconds() / 60
