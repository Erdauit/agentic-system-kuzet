"""Манифест модели в реестре (SPEC.md §2.5, §9.1 п. 9, models/registry/)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
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
