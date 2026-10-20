"""Автопоиск и зондирование камер (SPEC.md §3.2, шаг 3).

Поиск: ONVIF WS-Discovery (multicast и адресные запросы по подсетям) и сканирование RTSP-портов.
Найденное устройство несёт кандидатов потоков; зондирование выбирает main и substream.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.camera import RtspUrl, StreamProbe, StreamRole
from shared.schemas.common import Contract


class DiscoveryMethod(StrEnum):
    ONVIF_MULTICAST = "onvif_multicast"
    ONVIF_UNICAST = "onvif_unicast"
    RTSP_SCAN = "rtsp_scan"


class CandidateSource(StrEnum):
    ONVIF_PROFILE = "onvif_profile"  # GetStreamUri профиля ONVIF
    VENDOR_TEMPLATE = "vendor_template"  # configs/camera_url_templates.yaml
    GENERIC = "generic"  # типовые пути, если производитель неизвестен


AuthState = Literal["ok", "failed", "not_required", "unknown"]


class StreamCandidate(Contract):
    url: RtspUrl
    source: CandidateSource
    role_hint: StreamRole | None = Field(default=None, description="Что ожидаем: main или sub")
    profile: str | None = Field(default=None, max_length=120, description="Профиль ONVIF")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    channel: int = Field(
        default=1, ge=1, le=256, description="Канал NVR (1 — для одиночной камеры)"
    )


class DiscoveredDevice(Contract):
    host: str = Field(min_length=1, max_length=255)
    methods: list[DiscoveryMethod] = Field(min_length=1)
    onvif_xaddr: str | None = Field(default=None, max_length=500)
    rtsp_port: int | None = Field(default=None, gt=0, lt=65536)
    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    firmware: str | None = Field(default=None, max_length=120)
    serial: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=200, description="Имя из ONVIF scopes")
    auth: AuthState = "unknown"
    channels: int = Field(default=1, ge=1, le=256)
    candidates: list[StreamCandidate] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CandidateProbe(Contract):
    url: RtspUrl
    ok: bool
    error: str | None = Field(default=None, max_length=500)
    probe: StreamProbe | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.ok != (self.probe is not None):
            raise ValueError("probe задаётся тогда и только тогда, когда ok")
        return self


class ProbeReport(Contract):
    """Итог зондирования камеры: выбранные main и sub и всё, что пробовали (для диагностики)."""

    host: str = Field(min_length=1, max_length=255)
    channel: int = Field(default=1, ge=1, le=256)
    main: StreamProbe | None = None
    sub: StreamProbe | None = None
    tried: list[CandidateProbe] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    probed_at: AwareDatetime

    @model_validator(mode="after")
    def _roles(self) -> Self:
        if self.main is not None and self.main.role is not StreamRole.MAIN:
            raise ValueError("main должен иметь role=main")
        if self.sub is not None and self.sub.role is not StreamRole.SUB:
            raise ValueError("sub должен иметь role=sub")
        return self


class DiscoveryJob(Contract):
    id: UUID
    status: Literal["running", "done", "failed"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    subnets: list[str] = Field(default_factory=list)
    hosts_total: int = Field(default=0, ge=0)
    hosts_scanned: int = Field(default=0, ge=0)
    devices: list[DiscoveredDevice] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def _finished(self) -> Self:
        if (self.status == "running") != (self.finished_at is None):
            raise ValueError("finished_at задаётся тогда и только тогда, когда поиск завершён")
        if self.status == "failed" and not self.error:
            raise ValueError("для failed нужна причина (error)")
        return self
