"""Мастер, шаг 2 (вердикт по железу) и PDF плана мощности — без БД."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core_api.capacity.hardware import gop_buffer_mb, hardware_verdict
from core_api.capacity.planner import CameraDemand, PlannerInputs, plan_capacity
from core_api.capacity.report import FontMissingError, render_pdf
from shared.config import QoimaConfig
from shared.schemas import BenchResult, DoctorReport, FitVerdict, ScenarioKind, StreamProbe
from tests.contract.examples import EXAMPLES


def bench(**hw: object) -> BenchResult:
    data = dict(EXAMPLES["BenchResult"])
    data["hardware"] = {**data["hardware"], **hw}
    return BenchResult.model_validate(data)


def probe(role: str, kbps: int | None) -> StreamProbe:
    return StreamProbe.model_validate(
        {
            "role": role,
            "url": "rtsp://10.0.0.1/a",
            "codec": "h264",
            "width": 640,
            "height": 360,
            "fps": 15,
            "bitrate_kbps": kbps,
            "probed_at": datetime.now(UTC),
        }
    )


def test_gop_buffer_estimate() -> None:
    cams = [[probe("main", 4096), probe("sub", 512)]] * 10
    # (4096 + 512) кбит/с × 60 с / 8 / 1024 ≈ 33,75 МБ на камеру
    assert gop_buffer_mb(cams, 60) == pytest.approx(337.5)
    assert gop_buffer_mb([[probe("main", None)]], 8) == pytest.approx(3.90625)


def test_hardware_verdict_levels(config: QoimaConfig) -> None:
    ok_verdict, items = hardware_verdict(None, bench(), config, [])
    assert ok_verdict is FitVerdict.FITS_WITH_LIMITATIONS  # нет отчёта doctor
    assert any(i.name == "Самодиагностика" for i in items)
    report = (
        DoctorReport.model_validate(EXAMPLES["DoctorReport"])
        if "DoctorReport" in EXAMPLES
        else None
    )
    no_gpu, items = hardware_verdict(
        report, bench(gpus=[], providers=["CPUExecutionProvider"]), config, []
    )
    assert no_gpu is FitVerdict.DOES_NOT_FIT and any(
        i.name == "GPU" and i.level == "fail" for i in items
    )
    small = bench(gpus=[{**EXAMPLES["BenchResult"]["hardware"]["gpus"][0], "vram_total_mb": 16000}])
    v, items = hardware_verdict(report, small, config, [])
    assert v is not FitVerdict.FITS and any("без локального VLM" in i.detail for i in items)
    tiny_ram = bench(ram_total_mb=8192)
    v, items = hardware_verdict(report, tiny_ram, config, [[probe("main", 8000)]] * 50)
    assert any(i.name == "ОЗУ" and i.level == "warn" for i in items)


def test_pdf_report_renders(config: QoimaConfig) -> None:
    b = bench()
    cams = tuple(
        CameraDemand(f"cam-{i}", (ScenarioKind.FALL,), "h264", 640, 360, 15) for i in range(3)
    )
    plan = plan_capacity(PlannerInputs(b, cams), config, datetime.now(UTC))
    try:
        pdf = render_pdf(plan, b, "Цех №3 — Қойма", {"cam-0": "Линия упаковки"})
    except FontMissingError:
        pytest.skip("нет шрифта с кириллицей на этой машине")
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000
