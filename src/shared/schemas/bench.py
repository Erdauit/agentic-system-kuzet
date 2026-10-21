"""Результат `qoima bench` (SPEC.md §3.2 шаг 6, Фаза 3): замеры на этом железе.

План мощности строится только из этих замеров (SPEC.md §2.8 «оптимизация — по замерам»):
пропускная способность декодирования по кодекам и разрешениям, задержка и пропускная
способность моделей при разных батчах, пропускная способность VLM на типовом запросе арбитра.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.capacity import GPUInfo
from shared.schemas.common import Contract, Identifier, UnitFloat


class HardwareFingerprint(Contract):
    """Что измерялось: при смене GPU, драйвера или версий библиотек план нужно пересчитать."""

    hostname: str = Field(min_length=1, max_length=255)
    cpu_model: str = Field(min_length=1, max_length=200)
    cpu_cores: int = Field(gt=0, description="Логических ядер")
    ram_total_mb: int = Field(gt=0)
    gpus: list[GPUInfo] = Field(default_factory=list)
    onnxruntime: str | None = None
    providers: list[str] = Field(default_factory=list, description="Доступные провайдеры ORT")
    libavcodec: str = Field(min_length=1, description="Версия FFmpeg (libavcodec)")
    pipeline_version: str = Field(min_length=1)

    @property
    def key(self) -> str:
        """Ключ сопоставимости прогонов: железо и версии, от которых зависят замеры."""
        gpus = ",".join(f"{g.name}/{g.driver_version}" for g in self.gpus) or "cpu"
        return (
            f"{self.cpu_model}|{self.cpu_cores}|{gpus}|ort={self.onnxruntime}|av={self.libavcodec}"
        )


class DecodeBackend(StrEnum):
    NVDEC = "nvdec"
    CPU = "cpu"


class DecodeMeasurement(Contract):
    """Сколько кадров в секунду этот узел декодирует и уменьшает до миниатюры детектора
    движения — то, что hot path делает с каждым кадром потока анализа."""

    codec: str = Field(min_length=1, max_length=20)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source_fps: float = Field(gt=0)
    backend: DecodeBackend
    gpu_index: int | None = Field(default=None, ge=0)
    parallel_streams: int = Field(gt=0, description="Число параллельных декодеров на пике")
    fps_total: float = Field(gt=0, description="Кадров/с суммарно на пике")
    cv: float = Field(ge=0, description="Коэффициент вариации между повторами")
    vram_per_stream_mb: int | None = Field(default=None, ge=0)
    source: Literal["camera", "synthetic"]
    camera_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _backend_gpu(self) -> Self:
        if (self.backend is DecodeBackend.NVDEC) != (self.gpu_index is not None):
            raise ValueError("gpu_index задаётся тогда и только тогда, когда backend=nvdec")
        return self

    @property
    def pixel_rate(self) -> float:
        return self.fps_total * self.width * self.height


class ModelMeasurement(Contract):
    model: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=120)
    runtime: Literal["onnxruntime", "tensorrt"]
    providers: list[str] = Field(min_length=1, description="Провайдеры, реально выбранные сессией")
    batch: int = Field(gt=0)
    latency_p50_ms: float = Field(gt=0)
    latency_p95_ms: float = Field(gt=0)
    throughput: float = Field(gt=0, description="Изображений (кропов) в секунду")
    cv: float = Field(ge=0)
    vram_mb: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _latency_order(self) -> Self:
        if self.latency_p95_ms < self.latency_p50_ms:
            raise ValueError("p95 меньше p50")
        return self


class VlmMeasurement(Contract):
    server: str = Field(min_length=1, max_length=40, description="vllm | sglang | unknown")
    model: str = Field(min_length=1, max_length=200)
    concurrency: int = Field(gt=0)
    requests: int = Field(gt=0)
    rps: float = Field(ge=0)
    latency_p50_s: float = Field(ge=0)
    latency_p95_s: float = Field(ge=0)
    valid_json_ratio: UnitFloat


class LiveLoad(Contract):
    """Нагрузка работающего hot path во время замера: бенчмарк его не останавливает, поэтому
    план считает измеренную ёмкость с поправкой на неё (решение Фазы 3, п. 3)."""

    cameras: int = Field(ge=0)
    decoded_fps: float = Field(ge=0)
    inferred_fps: float = Field(ge=0)


class BenchResult(Contract):
    id: str = Field(pattern=r"^bench-\d{8}-\d{6}$")
    started_at: AwareDatetime
    finished_at: AwareDatetime
    hardware: HardwareFingerprint
    repeats: int = Field(gt=0)
    decode: list[DecodeMeasurement] = Field(default_factory=list)
    models: list[ModelMeasurement] = Field(default_factory=list)
    vlm: list[VlmMeasurement] = Field(default_factory=list)
    live_load: LiveLoad | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _times(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at раньше started_at")
        return self
