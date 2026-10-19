"""VLM-арбитр: вердикт и итог арбитража (SPEC.md §9.1, п. 1).

Инвариант fail-open (SPEC.md §2.1) выражен прямо в типах: у вердикта нет варианта
«подавить алерт», а при отказе VLM или в shadow-режиме приоритет не меняется.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract


class HazardAssessment(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    CRITICAL = "CRITICAL"


class PriorityChange(StrEnum):
    """VLM может только понизить или повысить приоритет. Варианта SUPPRESS нет намеренно."""

    NONE = "NONE"
    LOWER = "LOWER"
    RAISE = "RAISE"


class VLMVerdict(Contract):
    """Ответ VLM. Числового поля confidence нет: самооценка VLM не является вероятностью."""

    scene_description: str = Field(min_length=1, max_length=1000)
    hazard_assessment: HazardAssessment
    suggested_priority_change: PriorityChange
    reasoning: str = Field(min_length=1, max_length=2000)
    consistency_ok: bool

    def consistency_problems(self) -> list[str]:
        """Проверка согласованности полей. Непустой список — вердикт нельзя применять."""
        problems: list[str] = []
        if not self.consistency_ok:
            problems.append("модель сама отметила вердикт как несогласованный")
        if (
            self.hazard_assessment is HazardAssessment.CRITICAL
            and self.suggested_priority_change is PriorityChange.LOWER
        ):
            problems.append("CRITICAL-опасность вместе с понижением приоритета")
        if (
            self.hazard_assessment is HazardAssessment.NONE
            and self.suggested_priority_change is PriorityChange.RAISE
        ):
            problems.append("опасности нет, но предлагается повысить приоритет")
        return problems


class ArbiterMode(StrEnum):
    SHADOW = "shadow"  # пишет вердикты, приоритеты не меняет
    ASSIST = "assist"  # может понизить/повысить приоритет, но не подавить


class FailOpenReason(StrEnum):
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    INCONSISTENT = "inconsistent"
    UNAVAILABLE = "unavailable"
    OVERLOADED = "overloaded"


class ArbiterOutcome(Contract):
    incident_id: UUID
    mode: ArbiterMode
    verdict: VLMVerdict | None = None
    fail_open_reason: FailOpenReason | None = None
    applied_priority_change: PriorityChange
    latency_seconds: float = Field(ge=0)
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _fail_open_invariants(self) -> Self:
        if (self.verdict is None) == (self.fail_open_reason is None):
            raise ValueError("должно быть ровно одно: verdict или fail_open_reason")
        if (
            self.fail_open_reason is not None
            and self.applied_priority_change is not PriorityChange.NONE
        ):
            raise ValueError("fail-open: алерт уходит как есть, приоритет не меняется")
        if (
            self.mode is ArbiterMode.SHADOW
            and self.applied_priority_change is not PriorityChange.NONE
        ):
            raise ValueError("shadow-режим не меняет приоритеты")
        if self.verdict is not None:
            if self.verdict.consistency_problems() and (
                self.applied_priority_change is not PriorityChange.NONE
            ):
                raise ValueError("несогласованный вердикт нельзя применять")
            if self.applied_priority_change not in (
                PriorityChange.NONE,
                self.verdict.suggested_priority_change,
            ):
                raise ValueError("применённое изменение приоритета не совпадает с вердиктом")
        return self
