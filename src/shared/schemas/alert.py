"""Алерты: доставка по каналам и системные алерты (SPEC.md §6.1, §7).

Два потока никогда не смешиваются: SAFETY (падение, зона, дым/огонь — операторам)
и SYSTEM (здоровье камер и компонентов — инженерам и site_admin).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from shared.schemas.common import Contract, Identifier


class AlertStream(StrEnum):
    SAFETY = "safety"
    SYSTEM = "system"


class ChannelKind(StrEnum):
    WEB_UI = "web_ui"
    TELEGRAM = "telegram"
    LOCAL_RELAY = "local_relay"
    OPCUA = "opcua"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    RETRYING = "retrying"
    FAILED = "failed"


def idempotency_key(
    stream: AlertStream,
    ref_id: UUID,
    channel: ChannelKind,
    target: str,
    escalation_level: int,
) -> str:
    """Детерминированный ключ доставки: повторная отправка того же алерта тому же получателю
    на том же уровне эскалации даёт тот же ключ (at-least-once без дублей у получателя)."""
    raw = f"{stream.value}|{ref_id}|{channel.value}|{target}|{escalation_level}"
    return hashlib.sha256(raw.encode()).hexdigest()


class AlertDelivery(Contract):
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    stream: AlertStream
    channel: ChannelKind
    target: str = Field(
        min_length=1, max_length=200, description="Получатель: chat id, реле, группа"
    )
    incident_id: UUID | None = None
    system_alert_id: UUID | None = None
    escalation_level: int = Field(ge=0, le=10)
    attempt: int = Field(ge=1)
    status: DeliveryStatus
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: AwareDatetime
    sent_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _stream_reference(self) -> Self:
        if self.stream is AlertStream.SAFETY and (
            self.incident_id is None or self.system_alert_id is not None
        ):
            raise ValueError("safety-доставка ссылается только на incident_id")
        if self.stream is AlertStream.SYSTEM and (
            self.system_alert_id is None or self.incident_id is not None
        ):
            raise ValueError("system-доставка ссылается только на system_alert_id")
        if (self.status is DeliveryStatus.SENT) != (self.sent_at is not None):
            raise ValueError("sent_at задаётся тогда и только тогда, когда status=sent")
        ref = self.incident_id or self.system_alert_id
        if ref is None:  # недостижимо после проверок выше; явная проверка нужна для mypy
            raise ValueError("нет ссылки на инцидент или системный алерт")
        expected = idempotency_key(
            self.stream, ref, self.channel, self.target, self.escalation_level
        )
        if self.idempotency_key != expected:
            raise ValueError("idempotency_key не соответствует полям доставки")
        return self


class SystemAlertKind(StrEnum):
    CAMERA_DOWN = "camera_down"
    CAMERA_FROZEN = "camera_frozen"
    CAMERA_BLACK = "camera_black"
    CAMERA_OVEREXPOSED = "camera_overexposed"
    CAMERA_BLUR = "camera_blur"
    CAMERA_OCCLUDED = "camera_occluded"
    CAMERA_VIEW_SHIFT = "camera_view_shift"
    COMPONENT_DOWN = "component_down"
    DISK_LOW = "disk_low"
    TIME_UNSYNCED = "time_unsynced"
    CANARY_FAILED = "canary_failed"
    VLM_DEGRADED = "vlm_degraded"


class SystemAlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class SystemAlert(Contract):
    id: UUID
    kind: SystemAlertKind
    severity: SystemAlertSeverity
    component: str = Field(min_length=1, max_length=80)
    camera_id: Identifier | None = None
    message: str = Field(min_length=1, max_length=1000, description="Понятный текст для инженера")
    details: dict[str, JsonValue] = Field(default_factory=dict)
    raised_at: AwareDatetime
    acknowledged_at: AwareDatetime | None = None
    acknowledged_by: str | None = None
    cleared_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _camera_kinds_need_camera(self) -> Self:
        if self.kind.value.startswith("camera_") and self.camera_id is None:
            raise ValueError("алерт по камере должен содержать camera_id")
        if self.cleared_at is not None and self.cleared_at < self.raised_at:
            raise ValueError("cleared_at раньше raised_at")
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("acknowledged_at и acknowledged_by задаются вместе")
        return self

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    @property
    def dedup_key(self) -> str:
        """Одновременно активен не больше одного алерта с таким ключом."""
        return system_alert_key(self.kind, self.component, self.camera_id)


def system_alert_key(kind: SystemAlertKind, component: str, camera_id: str | None) -> str:
    return f"{kind.value}:{component}:{camera_id or ''}"
