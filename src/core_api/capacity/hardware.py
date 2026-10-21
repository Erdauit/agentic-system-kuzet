"""Мастер, шаг 2: «подходит / подходит с ограничениями / не подходит» (SPEC.md §3.2).

Источники: отчёт `qoima doctor --report` (драйвер, CUDA, Docker, диск, время — снаружи
контейнера) и последний `qoima bench` (GPU, VRAM, NVDEC, CPU, ОЗУ). Память сверяется с оценкой
GOP-буферов: (битрейт main + sub) × pipeline.gop_buffer_seconds по каждой камере.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.config import QoimaConfig
from shared.schemas import BenchResult, DoctorLevel, DoctorReport, FitVerdict, StreamProbe

LEVEL_ORDER = {"ok": 0, "warn": 1, "fail": 2}


@dataclass(frozen=True)
class HardwareItem:
    name: str
    level: str  # ok | warn | fail
    detail: str


def gop_buffer_mb(probes_by_camera: list[list[StreamProbe]], seconds: float) -> float:
    """Сжатое видео в RAM: сумма битрейтов потоков камеры × длина буфера. Без битрейта — 4 Мбит/с
    на main и 0,5 Мбит/с на sub (типовые значения камер, docs/ASSUMPTIONS.md)."""
    total_kbps = 0.0
    for probes in probes_by_camera:
        for p in probes:
            default = 4000 if p.role.value == "main" else 500
            total_kbps += p.bitrate_kbps if p.bitrate_kbps else default
    return total_kbps * seconds / 8 / 1024


def hardware_verdict(
    report: DoctorReport | None,
    bench: BenchResult | None,
    config: QoimaConfig,
    probes_by_camera: list[list[StreamProbe]],
) -> tuple[FitVerdict, list[HardwareItem]]:
    d = config.defaults
    items: list[HardwareItem] = []
    if report is None:
        items.append(
            HardwareItem(
                "Самодиагностика", "warn", "нет отчёта: на сервере выполните qoima doctor --report"
            )
        )
    else:
        for o in report.outcomes:
            if o.level is DoctorLevel.FAIL:
                items.append(
                    HardwareItem(o.name, "fail", f"{o.detail}. {o.recommendation}".strip(". "))
                )
            elif o.level is DoctorLevel.WARN:
                items.append(
                    HardwareItem(o.name, "warn", f"{o.detail}. {o.recommendation}".strip(". "))
                )
    if bench is None:
        items.append(HardwareItem("Бенчмарк", "warn", "нет замера: выполните qoima bench (шаг 6)"))
    else:
        hw = bench.hardware
        if not hw.gpus:
            items.append(
                HardwareItem(
                    "GPU", "fail", "GPU не найден — подходит только для разработки и проверки"
                )
            )
        for g in hw.gpus:
            if g.vram_total_mb < d.vlm.min_vram_gb * 1024:
                items.append(
                    HardwareItem(
                        f"GPU{g.index}",
                        "warn",
                        f"{g.name}: {g.vram_total_mb // 1024} ГБ VRAM < {d.vlm.min_vram_gb:g} ГБ — без локального VLM (алерты без арбитража)",
                    )
                )
            else:
                items.append(
                    HardwareItem(
                        f"GPU{g.index}",
                        "ok",
                        f"{g.name}, {g.vram_total_mb // 1024} ГБ VRAM, драйвер {g.driver_version}",
                    )
                )
            if not g.nvdec_available:
                items.append(
                    HardwareItem(
                        f"NVDEC GPU{g.index}",
                        "warn",
                        "аппаратное декодирование недоступно — видео декодируется на CPU",
                    )
                )
        if hw.gpus and not any(p.startswith(("Tensorrt", "CUDA")) for p in hw.providers):
            items.append(
                HardwareItem(
                    "Инференс на GPU",
                    "warn",
                    "в образе нет onnxruntime-gpu — модели работают на CPU (GPU-профиль не собран?)",
                )
            )
        need = d.capacity.ram_base_mb + gop_buffer_mb(
            probes_by_camera, d.pipeline.gop_buffer_seconds
        )
        limit = hw.ram_total_mb * d.capacity.ram_max_ratio
        level = "ok" if need <= limit else "warn"
        items.append(
            HardwareItem(
                "ОЗУ",
                level,
                f"нужно ≈ {need / 1024:.1f} ГБ (сервисы + видеобуфер {len(probes_by_camera)} камер) из {hw.ram_total_mb / 1024:.0f} ГБ; допустимо {d.capacity.ram_max_ratio:.0%}",
            )
        )
        items.append(HardwareItem("CPU", "ok", f"{hw.cpu_model}, {hw.cpu_cores} ядер"))
    worst = max((LEVEL_ORDER[i.level] for i in items), default=0)
    verdict = {0: FitVerdict.FITS, 1: FitVerdict.FITS_WITH_LIMITATIONS, 2: FitVerdict.DOES_NOT_FIT}[
        worst
    ]
    return verdict, items
