"""Здоровье компонентов для раздела «Состояние системы» (SPEC.md §6.1)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract


class HealthStatus(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    UNKNOWN = "unknown"
    # Компонент сознательно не развёрнут в этой конфигурации (например, нет GPU-профиля).
    NOT_DEPLOYED = "not_deployed"


# Порядок «хуже — больше». UNKNOWN хуже YELLOW: «тишина ≠ норма» (SPEC.md §2.2).
_SEVERITY_ORDER: dict[HealthStatus, int] = {
    HealthStatus.NOT_DEPLOYED: -1,
    HealthStatus.GREEN: 0,
    HealthStatus.YELLOW: 1,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.RED: 3,
}


def worst_status(statuses: Iterable[HealthStatus]) -> HealthStatus:
    considered = [s for s in statuses if s is not HealthStatus.NOT_DEPLOYED]
    if not considered:
        return HealthStatus.UNKNOWN
    return max(considered, key=lambda s: _SEVERITY_ORDER[s])


class ComponentKind(StrEnum):
    CAMERAS = "cameras"
    PIPELINE = "pipeline"
    MODELS = "models"
    TRITON = "triton"
    LLM = "llm"
    QUEUES = "queues"
    DATABASE = "database"
    VECTOR_DB = "vector_db"
    MEDIA_SERVER = "media_server"
    AGENTS = "agents"
    WORKER = "worker"
    DISK = "disk"
    TIME = "time"
    ALERT_CHANNELS = "alert_channels"


DetailValue = str | int | float | bool | None


class ComponentHealth(Contract):
    component: str = Field(min_length=1, max_length=80)
    kind: ComponentKind
    status: HealthStatus
    message: str = Field(min_length=1, max_length=500, description="Понятный текст для человека")
    checked_at: AwareDatetime
    latency_ms: float | None = Field(default=None, ge=0)
    details: dict[str, DetailValue] = Field(default_factory=dict)


class SystemHealth(Contract):
    status: HealthStatus
    components: list[ComponentHealth]
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _status_is_worst(self) -> Self:
        expected = worst_status(c.status for c in self.components)
        if self.status is not expected:
            raise ValueError(f"общий статус должен быть {expected.value}")
        return self

    @classmethod
    def from_components(cls, components: list[ComponentHealth], generated_at: datetime) -> Self:
        return cls(
            status=worst_status(c.status for c in components),
            components=components,
            generated_at=generated_at,
        )
