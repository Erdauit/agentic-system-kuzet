"""События hot path (SPEC.md §5): выход детерминированных FSM."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import BBox, Contract, Identifier, UnitFloat


class EventType(StrEnum):
    FALL = "fall"
    RED_ZONE = "red_zone"
    SMOKE_FIRE = "smoke_fire"
    # FallFSM: человек упал, но встал до истечения lying_seconds — микро-инцидент без алерта.
    FALL_RECOVERED = "fall_recovered"


# Классы безопасности: по ним фидбек и агенты не могут снизить чувствительность (SPEC.md §2.6).
SAFETY_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.FALL, EventType.RED_ZONE, EventType.SMOKE_FIRE}
)


class RoutingDecision(StrEnum):
    """Маршрутизация по уверенности (SPEC.md §5.10). Ниже T_low событие не создаётся вовсе."""

    EVENT = "event"  # P > T_high
    EVENT_WITH_ARBITER = "event_with_arbiter"  # T_low <= P <= T_high: событие + запрос арбитру


class SyntheticKind(StrEnum):
    """Служебные события, которые проходят весь путь до каналов (SPEC.md §3.2 шаг 8, §6.1).

    selftest — сквозной самотест из мастера: уходит во все каналы, человек подтверждает получение;
    canary — периодическая канарейка: только в системный канал, операторов не беспокоит.
    """

    SELFTEST = "selftest"
    CANARY = "canary"


class FrameRef(Contract):
    """Ссылка на кадр в BlobStore (ключевой кадр, кадр-доказательство)."""

    blob_key: str = Field(min_length=1)
    captured_at: AwareDatetime
    bbox: BBox | None = None


class Event(Contract):
    id: UUID
    type: EventType
    camera_id: Identifier
    zone_id: Identifier | None = None
    track_ids: list[int] = Field(default_factory=list)
    started_at: AwareDatetime = Field(description="Начало ситуации (например, момент падения)")
    detected_at: AwareDatetime = Field(description="Момент, когда FSM перешёл в ALERT")
    detector_score: UnitFloat = Field(description="Калиброванная вероятность детектора")
    routing: RoutingDecision
    model_version: str = Field(min_length=1, max_length=120)
    evidence: list[FrameRef] = Field(default_factory=list, max_length=16)
    area: Identifier | None = Field(
        default=None, description="Участок зоны — ключ дедупликации между камерами"
    )
    synthetic: SyntheticKind | None = None

    @model_validator(mode="after")
    def _timestamps(self) -> Self:
        if self.detected_at < self.started_at:
            raise ValueError("detected_at раньше started_at")
        return self

    @property
    def is_safety(self) -> bool:
        return self.type in SAFETY_EVENT_TYPES
