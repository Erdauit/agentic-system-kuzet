"""Модели запросов и ответов HTTP API core-api (не межсервисные контракты — те в shared.schemas)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shared.schemas import (
    CameraConfig,
    CameraHealth,
    CameraHealthStatus,
    DiscoveredDevice,
    ProbeReport,
    QualityBaseline,
    Role,
    ScenarioKind,
    StreamCandidate,
    StreamProbe,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CredentialsIn(ApiModel):
    username: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(max_length=200)


# ---------------------------------------------------------------- вход и пользователи


class LoginIn(ApiModel):
    username: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=200)


class ChangePasswordIn(ApiModel):
    current_password: SecretStr = Field(min_length=1, max_length=200)
    new_password: SecretStr = Field(min_length=1, max_length=200)


class UserOut(ApiModel):
    id: uuid.UUID
    username: str
    display_name: str
    role: Role
    is_active: bool = True
    must_change_password: bool


class UserCreateIn(ApiModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._\-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=200)
    role: Role


class UserPatchIn(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None


class TemporaryPasswordOut(ApiModel):
    username: str
    temporary_password: str = Field(
        description="Показывается один раз; при первом входе нужно сменить"
    )


# ---------------------------------------------------------------- камеры


class CameraOut(ApiModel):
    camera: CameraConfig
    status: CameraHealthStatus
    health: CameraHealth | None
    baseline: QualityBaseline | None
    probes: list[StreamProbe]
    has_credentials: bool
    live_available: bool


class CameraCreateIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    main_url: str = Field(min_length=8, max_length=500)
    sub_url: str | None = Field(default=None, max_length=500)
    credentials: CredentialsIn | None = None
    location: str | None = Field(default=None, max_length=200)
    scenarios: list[ScenarioKind] = Field(default_factory=list)


class CameraPatchIn(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    scenarios: list[ScenarioKind] | None = None


class CameraCreatedOut(ApiModel):
    camera: CameraConfig
    report: ProbeReport


class DiscoveryIn(ApiModel):
    subnets: list[str] = Field(default_factory=list, max_length=64)
    credentials: list[CredentialsIn] = Field(default_factory=list, max_length=10)


class ProbeIn(ApiModel):
    host: str = Field(min_length=1, max_length=255)
    candidates: list[StreamCandidate] = Field(min_length=1, max_length=64)
    channel: int = Field(default=1, ge=1, le=256)
    credentials: CredentialsIn | None = None
    discovery_job_id: uuid.UUID | None = Field(
        default=None, description="Взять учётные данные, подошедшие при поиске"
    )


class AddDiscoveredIn(ApiModel):
    hosts: list[str] = Field(min_length=1, max_length=512)


class AddResult(ApiModel):
    host: str
    ok: bool
    camera_id: str | None = None
    name: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ImportIn(ApiModel):
    format: Literal["csv", "yaml"]
    content: str = Field(min_length=1, max_length=2_000_000)


class DiscoveryDevicesOut(ApiModel):
    devices: list[DiscoveredDevice]
    already_added: list[str]


class HistoryOut(ApiModel):
    camera_id: str
    metric: str
    range: str
    points: list[tuple[float, float]]


# ---------------------------------------------------------------- мастер


class ShiftIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class SiteIn(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    ui_languages: list[Literal["ru", "kk"]] = Field(min_length=1)
    default_language: Literal["ru", "kk"]
    timezone: str = Field(min_length=1, max_length=64)
    shifts: list[ShiftIn] = Field(min_length=1, max_length=8)
    discovery_subnets: list[str] = Field(default_factory=list, max_length=64)


class TelegramTokenIn(ApiModel):
    token: SecretStr = Field(min_length=10, max_length=200)


class ChannelTestIn(ApiModel):
    channel: Literal["web_ui", "telegram", "local_relay"]
    stream: Literal["safety", "system"] = "system"


class DeliveryOut(ApiModel):
    channel: str
    target: str
    ok: bool
    error: str | None = None
