"""TaskSpec — спецификация задачи мониторинга от агента Architect (SPEC.md §9.1, п. 5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, DisplayName, Identifier, Polygon


class TaskType(StrEnum):
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    KEYPOINTS = "keypoints"
    ANOMALY = "anomaly"
    CLASSIFICATION = "classification"


class Feasibility(StrEnum):
    FEASIBLE = "feasible"
    FEASIBLE_WITH_LIMITATIONS = "feasible_with_limitations"
    INFEASIBLE = "infeasible"


class ClassDefinition(Contract):
    name: Identifier
    definition: str = Field(
        min_length=1, max_length=1000, description="Что считается классом, а что нет"
    )


SuccessMetric = Literal["f1_macro", "f1_micro", "precision", "recall", "roc_auc", "brier", "ece"]


class SuccessCriterion(Contract):
    metric: SuccessMetric
    comparator: Literal["gte", "lte"]
    threshold: float

    @model_validator(mode="after")
    def _direction(self) -> Self:
        # Для ошибок калибровки (brier, ece) «лучше» — меньше; для остальных — больше.
        lower_is_better = self.metric in ("brier", "ece")
        if lower_is_better != (self.comparator == "lte"):
            raise ValueError(
                f"для {self.metric} ожидается comparator={'lte' if lower_is_better else 'gte'}"
            )
        return self


class FeasibilityAssessment(Contract):
    verdict: Feasibility
    reasoning: str = Field(min_length=1, max_length=4000)
    alternatives: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infeasible_needs_alternative(self) -> Self:
        # Если задача нереализуема на CCTV, агент обязан прямо сказать и предложить альтернативу.
        if self.verdict is Feasibility.INFEASIBLE and not self.alternatives:
            raise ValueError("нереализуемая задача должна содержать альтернативы")
        return self


class TaskSpec(Contract):
    id: UUID
    title: DisplayName
    request_text: str = Field(min_length=1, max_length=4000, description="Исходный запрос клиента")
    task_type: TaskType
    camera_ids: list[Identifier] = Field(min_length=1)
    roi: Polygon | None = None
    target_fps: float = Field(gt=0, le=60)
    classes: list[ClassDefinition] = Field(min_length=1)
    success_criteria: list[SuccessCriterion] = Field(min_length=1)
    feasibility: FeasibilityAssessment
    target_samples_per_class: int = Field(gt=0)
    gpu_hours_budget: float = Field(gt=0)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _unique_classes(self) -> Self:
        names = [c.name for c in self.classes]
        if len(set(names)) != len(names):
            raise ValueError("имена классов повторяются")
        if self.task_type is TaskType.CLASSIFICATION and len(names) < 2:
            raise ValueError("классификация требует минимум двух классов")
        return self
