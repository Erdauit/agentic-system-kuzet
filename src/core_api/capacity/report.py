"""PDF-отчёт плана мощности (SPEC.md §3.2 шаг 6: «отчёт экспортируется в PDF — пригодится для КП»).

ReportLab (BSD-3-Clause). Кириллица и казахские буквы — шрифтом DejaVu Sans (в образе core-api —
пакет fonts-dejavu-core); на машине разработчика берётся первый найденный Unicode-шрифт.
"""

from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from shared.schemas import BenchResult, CapacityPlan, FitVerdict

FONT = "QoimaSans"
FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
)
VERDICT_RU = {
    FitVerdict.FITS: "подходит",
    FitVerdict.FITS_WITH_LIMITATIONS: "подходит с ограничениями",
    FitVerdict.DOES_NOT_FIT: "не подходит",
}
SCENARIO_RU = {"fall": "Падение", "red_zone": "Опасная зона", "smoke_fire": "Дым / огонь"}


class FontMissingError(RuntimeError):
    """Нет шрифта с кириллицей — PDF был бы нечитаемым."""


def _font() -> str:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return FONT
    for path in FONT_CANDIDATES:
        if path.is_file():
            pdfmetrics.registerFont(TTFont(FONT, str(path)))
            return FONT
    raise FontMissingError(
        "не найден шрифт с кириллицей (DejaVu Sans): установите fonts-dejavu-core"
    )


def _fmt(value: float, unit: str) -> str:
    if unit == "доля":
        return f"{value:.0%}"
    if unit == "запр/с":
        return f"{value:.3f} {unit}"
    return f"{value:,.0f} {unit}".replace(",", " ")


def _table(rows: list[list[str]], widths: list[float], font: str) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), font, 8.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe9f7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa5b1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return t


def render_pdf(
    plan: CapacityPlan, bench: BenchResult | None, site: str, camera_names: dict[str, str]
) -> bytes:
    font = _font()
    h1 = ParagraphStyle("h1", fontName=font, fontSize=15, leading=19, spaceAfter=6)
    h2 = ParagraphStyle("h2", fontName=font, fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("body", fontName=font, fontSize=9, leading=12)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"План мощности — {site}",
        author="Qoima Vision",
    )
    w = A4[0] - 32 * mm
    story: list[object] = [
        Paragraph(f"План мощности — {site}", h1),
        Paragraph(
            f"Итог: <b>{VERDICT_RU[plan.verdict]}</b>. Составлен {plan.created_at:%d.%m.%Y %H:%M} UTC "
            f"по замеру {plan.benchmark_id}; backend {plan.backend}.",
            body,
        ),
    ]
    if bench is not None:
        hw = bench.hardware
        gpus = (
            ", ".join(
                f"{g.name} ({g.vram_total_mb // 1024} ГБ, драйвер {g.driver_version})"
                for g in hw.gpus
            )
            or "нет GPU"
        )
        story += [
            Spacer(1, 3 * mm),
            Paragraph(
                f"Сервер: {hw.cpu_model} (ядер: {hw.cpu_cores}), ОЗУ {hw.ram_total_mb // 1024} ГБ; GPU: {gpus}.",
                body,
            ),
        ]

    story.append(Paragraph("Загрузка ресурсов", h2))
    rows = [["Ресурс", "По плану", "Допустимо", "Замер"]]
    for u in plan.utilization:
        rows.append(
            [
                u.resource,
                _fmt(u.used, u.unit),
                _fmt(u.capacity, u.unit),
                "да" if u.measured else "нет",
            ]
        )
    story.append(_table(rows, [w * 0.4, w * 0.2, w * 0.2, w * 0.2], font))

    story.append(Paragraph("Камеры и FPS анализа", h2))
    rows = [["Камера", "Сценарий", "Целевой FPS", "По плану", "Без движения"]]
    for a in plan.allocations:
        rows.append(
            [
                camera_names.get(a.camera_id, a.camera_id),
                SCENARIO_RU.get(a.scenario.value, a.scenario.value),
                f"{a.target_fps:g}",
                f"{a.fps_active:g}" + (" (понижен)" if a.degraded else ""),
                f"{a.fps_idle:g}",
            ]
        )
    if len(rows) == 1:
        rows.append(["—", "сценарии не включены или модели не установлены", "", "", ""])
    story.append(_table(rows, [w * 0.32, w * 0.2, w * 0.16, w * 0.18, w * 0.14], font))

    if plan.vram:
        story.append(Paragraph("Видеопамять, МБ", h2))
        rows = [["GPU", "Всего", "Пайплайн", "Triton", "VLM", "Обучение", "Запас"]]
        for v in plan.vram:
            rows.append(
                [
                    str(v.gpu_index),
                    *(
                        str(x)
                        for x in (
                            v.total_mb,
                            v.pipeline_mb,
                            v.triton_mb,
                            v.llm_mb,
                            v.worker_mb,
                            v.reserve_mb,
                        )
                    ),
                ]
            )
        story.append(_table(rows, [w / 7] * 7, font))

    story.append(Paragraph("VLM-арбитр и серая зона", h2))
    if plan.vlm is not None:
        story.append(
            Paragraph(
                f"Ёмкость {plan.vlm.measured_rps:.2f} запр/с, ожидается {plan.vlm.expected_rps:.3f} запр/с "
                f"({plan.vlm.load_ratio:.0%} при допустимых {plan.vlm.max_load_ratio:.0%}); "
                f"gpu_memory_utilization = {plan.llm_gpu_memory_utilization}.",
                body,
            )
        )
    else:
        story.append(
            Paragraph(
                "Без локального VLM: события серой зоны уходят без арбитража (fail-open).", body
            )
        )
    rows = [["Сценарий", "t_low", "t_high"]]
    for s, pair in plan.gray_zone.items():
        rows.append([SCENARIO_RU.get(s.value, s.value), f"{pair.t_low:.3f}", f"{pair.t_high:.3f}"])
    story.append(_table(rows, [w * 0.5, w * 0.25, w * 0.25], font))

    if plan.warnings:
        story.append(Paragraph("Предупреждения", h2))
        for warn in plan.warnings:
            story.append(Paragraph(f"• {warn}", body))

    if bench is not None and (bench.decode or bench.models):
        story.append(Paragraph("Замеры бенчмарка", h2))
        rows = [["Декодирование", "Где", "Потоков", "Кадров/с", "Разброс"]]
        for d in bench.decode:
            rows.append(
                [
                    f"{d.codec} {d.width}×{d.height}",
                    d.backend.value,
                    str(d.parallel_streams),
                    f"{d.fps_total:.0f}",
                    f"{d.cv:.1%}",
                ]
            )
        story.append(_table(rows, [w * 0.3, w * 0.15, w * 0.15, w * 0.2, w * 0.2], font))
        if bench.models:
            story.append(Spacer(1, 3 * mm))
            rows = [["Модель", "Батч", "p95, мс", "Изобр/с", "Исполнитель"]]
            for m in bench.models:
                rows.append(
                    [
                        m.model,
                        str(m.batch),
                        f"{m.latency_p95_ms:.1f}",
                        f"{m.throughput:.0f}",
                        m.providers[0].removesuffix("ExecutionProvider"),
                    ]
                )
            story.append(_table(rows, [w * 0.3, w * 0.1, w * 0.15, w * 0.15, w * 0.3], font))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"Входы плана (sha256): {plan.inputs_digest}", body))
    doc.build(story)
    return buf.getvalue()
