"""План мощности (SPEC.md §3.2 шаг 6, §4.1, §5.10, Фаза 3).

Здесь только контракт и его инварианты; расчёт — core_api/capacity/planner.py по замерам
`qoima bench` (shared.schemas.bench).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, Identifier, ScenarioKind, UnitFloat


class GPUInfo(Contract):
    index: int = Field(ge=0)
    name: str = Field(min_length=1)
    vram_total_mb: int = Field(gt=0)
    nvdec_available: bool
    driver_version: str = Field(min_length=1)
    cuda_version: str = Field(min_length=1)


class CameraAllocation(Contract):
    camera_id: Identifier
    scenario: ScenarioKind
    fps_active: float = Field(gt=0, description="FPS при наличии движения")
    fps_idle: float = Field(gt=0, description="«Сторожевой» FPS без движения")
    target_fps: float = Field(gt=0, description="Целевой FPS сценария из конфига")
    gpu_index: int | None = Field(default=None, ge=0, description="None — инференс на CPU")

    @model_validator(mode="after")
    def _fps_order(self) -> Self:
        if self.fps_idle > self.fps_active:
            raise ValueError("fps_idle не может превышать fps_active")
        return self

    @property
    def degraded(self) -> bool:
        """Камера получила FPS ниже целевого — UI должен это показать."""
        return self.fps_active < self.target_fps


class VramBudget(Contract):
    gpu_index: int = Field(ge=0)
    total_mb: int = Field(gt=0)
    pipeline_mb: int = Field(ge=0)
    triton_mb: int = Field(ge=0)
    llm_mb: int = Field(ge=0)
    worker_mb: int = Field(ge=0)
    reserve_mb: int = Field(ge=0)

    @model_validator(mode="after")
    def _fits(self) -> Self:
        used = self.pipeline_mb + self.triton_mb + self.llm_mb + self.worker_mb + self.reserve_mb
        if used > self.total_mb:
            raise ValueError(
                f"GPU{self.gpu_index}: бюджет VRAM {used} МБ больше доступных {self.total_mb} МБ"
            )
        return self


class VlmCapacity(Contract):
    measured_rps: float = Field(
        gt=0, description="Измеренная пропускная способность VLM, запросов/с"
    )
    expected_rps: float = Field(ge=0, description="Ожидаемый поток запросов арбитру")
    max_load_ratio: UnitFloat = Field(description="Допустимая доля загрузки (по умолчанию 0.7)")

    @property
    def load_ratio(self) -> float:
        return self.expected_rps / self.measured_rps

    @model_validator(mode="after")
    def _within_capacity(self) -> Self:
        if self.load_ratio > self.max_load_ratio:
            raise ValueError(
                f"ожидаемая загрузка VLM {self.load_ratio:.0%} превышает допустимую "
                f"{self.max_load_ratio:.0%} — сузьте серую зону или понизьте FPS"
            )
        return self


class ThresholdPair(Contract):
    t_low: UnitFloat
    t_high: UnitFloat

    @model_validator(mode="after")
    def _order(self) -> Self:
        if not self.t_low < self.t_high:
            raise ValueError("ожидается t_low < t_high")
        return self


class ResourceUse(Contract):
    """Загрузка ресурса по плану: used/capacity ≤ доли использования из конфига."""

    resource: str = Field(min_length=1, max_length=60)
    used: float = Field(ge=0)
    capacity: float = Field(ge=0)
    unit: str = Field(min_length=1, max_length=20)
    measured: bool = Field(description="False — ёмкость не измерена (нет замера или модели)")

    @property
    def ratio(self) -> float:
        return self.used / self.capacity if self.capacity > 0 else 0.0


class PlanStatus(StrEnum):
    """Жизненный цикл плана: применяет только человек (решение Фазы 3, п. 5)."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class FitVerdict(StrEnum):
    FITS = "fits"
    FITS_WITH_LIMITATIONS = "fits_with_limitations"
    DOES_NOT_FIT = "does_not_fit"


class CapacityPlan(Contract):
    id: UUID
    created_at: AwareDatetime
    benchmark_id: str = Field(
        min_length=1, description="Прогон `qoima bench`, на котором основан план"
    )
    backend: Literal["lite", "deepstream"]
    gpus: list[GPUInfo] = Field(default_factory=list, description="Пусто — узел без GPU (CPU)")
    allocations: list[CameraAllocation]
    vram: list[VramBudget] = Field(default_factory=list)
    vlm: VlmCapacity | None = Field(default=None, description="None — режим без локального VLM")
    llm_gpu_memory_utilization: UnitFloat | None = None
    gray_zone: dict[ScenarioKind, ThresholdPair]
    verdict: FitVerdict
    warnings: list[str] = Field(default_factory=list)
    utilization: list[ResourceUse] = Field(default_factory=list)
    inputs_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 входных данных: те же замеры, камеры и конфиг → тот же план",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        gpu_ids = {g.index for g in self.gpus}
        if {v.gpu_index for v in self.vram} != gpu_ids:
            raise ValueError("бюджет VRAM должен быть задан для каждого GPU")
        if any(a.gpu_index is not None and a.gpu_index not in gpu_ids for a in self.allocations):
            raise ValueError("камера распределена на несуществующий GPU")
        if not self.gpus and any(a.gpu_index is not None for a in self.allocations):
            raise ValueError("на узле без GPU камеры распределяются на CPU (gpu_index=None)")
        if (self.vlm is None) != (self.llm_gpu_memory_utilization is None):
            raise ValueError(
                "llm_gpu_memory_utilization задаётся тогда и только тогда, когда есть VLM"
            )
        llm_mb = sum(v.llm_mb for v in self.vram)
        if self.vlm is None and llm_mb > 0:
            raise ValueError("без VLM бюджет llm_mb должен быть 0")
        degraded = any(a.degraded for a in self.allocations)
        if self.verdict is FitVerdict.FITS and (degraded or self.vlm is None or not self.gpus):
            raise ValueError(
                "есть пониженный FPS, нет VLM или нет GPU — вердикт не может быть fits"
            )
        return self
