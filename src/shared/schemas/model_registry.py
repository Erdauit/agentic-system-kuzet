"""Манифест модели в реестре (SPEC.md §2.5, §9.1 п. 9, models/registry/)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, Identifier, Sha256Hex
from shared.schemas.task_spec import TaskType


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


# Автодеплоя нет: candidate → shadow → active только через человека; откат — active ↔ retired.
MODEL_TRANSITIONS: dict[ModelStatus, frozenset[ModelStatus]] = {
    ModelStatus.CANDIDATE: frozenset({ModelStatus.SHADOW, ModelStatus.REJECTED}),
    ModelStatus.SHADOW: frozenset({ModelStatus.ACTIVE, ModelStatus.REJECTED}),
    ModelStatus.ACTIVE: frozenset({ModelStatus.RETIRED}),
    ModelStatus.RETIRED: frozenset({ModelStatus.ACTIVE}),
    ModelStatus.REJECTED: frozenset(),
}


class ModelSource(StrEnum):
    PRETRAINED = "pretrained"  # базовые веса (RF-DETR COCO и т.п.)
    QOIMA = "qoima"  # веса Qoima, поставляемые в models/registry/
    TRAINED = "trained"  # обучены на объекте AutoML-графом


class ModelRuntime(Contract):
    """Как запускать артефакт модели (ONNX): вход, нормализация, разбор выходов.

    Значения берутся из документации/исходников модели при подготовке артефакта
    (tools/models/), а не подбираются на глаз (SPEC.md §13.3).
    """

    format: Literal["onnx"]
    input_name: str = Field(min_length=1)
    input_width: int = Field(gt=0, le=4096)
    input_height: int = Field(gt=0, le=4096)
    color: Literal["rgb", "bgr"]
    mean: tuple[float, float, float] = Field(description="Вычитается после перевода в шкалу scale")
    std: tuple[float, float, float]
    scale: float = Field(gt=0, description="Делитель пикселей перед нормализацией: 255 или 1")
    outputs: list[str] = Field(min_length=1)
    dynamic_batch: bool
    decoder: Literal["rfdetr", "rtmpose_simcc", "detector_generic"]
    class_ids: dict[str, int] = Field(
        default_factory=dict, description="Имя класса → индекс выхода"
    )
    keypoints: int | None = Field(default=None, gt=0, le=256)
    simcc_split_ratio: float | None = Field(default=None, gt=0)
    crop_padding: float | None = Field(
        default=None, gt=1.0, le=2.0, description="Расширение бокса для top-down позы"
    )


class ModelManifest(Contract):
    name: Identifier
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.\-]+)?$")
    task_type: TaskType
    framework: str = Field(min_length=1, max_length=60)
    license: str = Field(min_length=1, max_length=120, description="SPDX-выражение лицензии весов")
    weights_sha256: Sha256Hex
    source: ModelSource
    training_task_id: UUID | None = None
    golden_set_version: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    status: ModelStatus
    created_at: AwareDatetime
    promoted_by: str | None = None
    runtime: ModelRuntime | None = None

    @model_validator(mode="after")
    def _human_in_the_loop(self) -> Self:
        if self.source is ModelSource.TRAINED:
            if self.training_task_id is None:
                raise ValueError("обученная модель должна ссылаться на задачу обучения")
            # Оценка только на golden set, размеченном человеком (SPEC.md §2.5).
            if self.status in (ModelStatus.SHADOW, ModelStatus.ACTIVE) and (
                self.golden_set_version is None or not self.metrics
            ):
                raise ValueError("в shadow/active — только после оценки на golden set")
        if (
            self.status is ModelStatus.ACTIVE
            and self.source is ModelSource.TRAINED
            and not self.promoted_by
        ):
            raise ValueError("выкатку в active подтверждает человек (promoted_by)")
        return self
