"""Модели конфигурации. Значения по умолчанию и пояснения «почему» — в configs/*.yaml.

В коде намеренно нет значений по умолчанию для порогов: единственный источник правды —
configs/defaults.yaml (SPEC.md §13.5 «никаких магических чисел в коде»).
"""

from __future__ import annotations

import ipaddress
import string
from datetime import time
from itertools import pairwise
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from shared.schemas.alert import ChannelKind
from shared.schemas.audit import Role
from shared.schemas.capacity import ThresholdPair
from shared.schemas.common import Contract, DisplayName, Identifier, Language, ScenarioKind

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
Ratio = Annotated[float, Field(gt=0, le=1)]


class Section(Contract):
    """Секция конфига: неизвестные ключи — ошибка (опечатка не должна молча игнорироваться)."""


# ---------------------------------------------------------------- defaults.yaml


class Shift(Section):
    name: DisplayName
    start: time
    end: time


class SiteConfig(Section):
    name: DisplayName
    ui_languages: list[Language] = Field(min_length=1)
    default_language: Language
    timezone: str
    shifts: list[Shift] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс: {self.timezone}") from exc
        if self.default_language not in self.ui_languages:
            raise ValueError("default_language должен входить в ui_languages")
        names = [s.name for s in self.shifts]
        if len(set(names)) != len(names):
            raise ValueError("названия смен повторяются")
        return self


class StorageConfig(Section):
    data_root: Path
    clips_retention_days: PositiveInt
    frames_retention_days: PositiveInt
    datasets_retention_days: PositiveInt


class SLOConfig(Section):
    fall_to_alert_p95_s: PositiveFloat
    red_zone_to_alert_p95_s: PositiveFloat
    smoke_fire_candidate_p95_s: PositiveFloat
    camera_offline_detection_s: PositiveFloat


class CameraHealthConfig(Section):
    check_interval_s: PositiveFloat
    stream_timeout_s: PositiveFloat
    low_fps_ratio: Ratio
    frozen_window_s: PositiveFloat
    black_luma_max: Annotated[float, Field(ge=0, le=255)]
    overexposed_luma_min: Annotated[float, Field(ge=0, le=255)]
    sharpness_drop_ratio: Ratio
    occlusion_ratio: Ratio
    view_shift_alert_score: Ratio
    raise_after_checks: Annotated[int, Field(ge=1, le=10)]
    clear_after_checks: Annotated[int, Field(ge=1, le=20)]
    frozen_max_mad: Annotated[float, Field(ge=0, le=10)]
    frozen_min_samples: Annotated[int, Field(ge=2, le=50)]
    analysis_width: Annotated[int, Field(ge=64, le=1280)]
    occlusion_grid: tuple[Annotated[int, Field(ge=2, le=32)], Annotated[int, Field(ge=2, le=32)]]
    occlusion_tile_texture_ratio: Ratio
    view_shift_orb_features: Annotated[int, Field(ge=50, le=5000)]
    view_shift_full_displacement: Ratio
    view_shift_min_inliers: Annotated[int, Field(ge=4, le=500)]
    view_shift_check_interval_s: PositiveFloat
    reconnect_backoff_min_s: PositiveFloat
    reconnect_backoff_max_s: PositiveFloat
    pipeline_silence_s: PositiveFloat

    @model_validator(mode="after")
    def _luma_order(self) -> Self:
        if self.black_luma_max >= self.overexposed_luma_min:
            raise ValueError("black_luma_max должен быть меньше overexposed_luma_min")
        if self.reconnect_backoff_min_s > self.reconnect_backoff_max_s:
            raise ValueError("reconnect_backoff_min_s больше reconnect_backoff_max_s")
        return self


class MotionConfig(Section):
    width: PositiveInt
    height: PositiveInt
    pixel_delta: Annotated[int, Field(gt=0, le=255)]
    changed_ratio: Ratio
    idle_fps: PositiveFloat


class BatchingConfig(Section):
    max_queue_delay_ms: Annotated[float, Field(gt=0, le=20)]  # SPEC.md §5.6: ≤ 20 мс


class PipelineConfig(Section):
    backend: Literal["lite", "deepstream"]
    substream_max_height: Annotated[int, Field(gt=0, le=720)]  # SPEC.md §5.1: ≤ 720p
    gop_buffer_seconds: PositiveFloat
    motion: MotionConfig
    batching: BatchingConfig
    deepstream_recommend_from_cameras: PositiveInt


class RoutingConfig(Section):
    vlm_max_load_ratio: Ratio


class VLMConfig(Section):
    arbiter_timeout_s: PositiveFloat
    max_output_retries: Annotated[int, Field(ge=0, le=2)]  # SPEC.md §9: ≤ 2 повтора
    shadow_days: PositiveInt
    min_vram_gb: PositiveFloat
    roi_frames: Annotated[int, Field(ge=3, le=4)]  # SPEC.md §9.1: 3–4 кадра


class IncidentsConfig(Section):
    dedup_window_s: PositiveFloat
    cooldown_s: PositiveFloat
    clip_pre_s: PositiveFloat
    clip_post_s: PositiveFloat
    keyframes_min: Annotated[int, Field(ge=1, le=6)]
    keyframes_max: Annotated[int, Field(ge=1, le=6)]

    @model_validator(mode="after")
    def _keyframes(self) -> Self:
        if self.keyframes_min > self.keyframes_max:
            raise ValueError("keyframes_min больше keyframes_max")
        return self


class CanaryConfig(Section):
    interval_minutes: PositiveFloat
    clip: Path


class HealthConfig(Section):
    component_timeout_s: PositiveFloat
    disk_warn_free_ratio: Ratio
    disk_critical_free_ratio: Ratio

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.disk_critical_free_ratio >= self.disk_warn_free_ratio:
            raise ValueError("disk_critical_free_ratio должен быть меньше disk_warn_free_ratio")
        return self


VersionString = Annotated[str, Field(pattern=r"^\d+(\.\d+){0,2}$")]


class DoctorConfig(Section):
    min_free_disk_gb: PositiveFloat
    max_clock_offset_ms: PositiveFloat
    min_driver_cuda_version: VersionString
    min_docker_version: VersionString
    min_compose_version: VersionString


class DiscoveryConfig(Section):
    subnets: list[str] = Field(default_factory=list)
    onvif_ports: list[Annotated[int, Field(gt=0, lt=65536)]] = Field(min_length=1)
    rtsp_ports: list[Annotated[int, Field(gt=0, lt=65536)]] = Field(min_length=1)
    ws_discovery_timeout_s: PositiveFloat
    connect_timeout_s: PositiveFloat
    onvif_timeout_s: PositiveFloat
    max_hosts: Annotated[int, Field(gt=0, le=65536)]
    concurrency: Annotated[int, Field(gt=0, le=1024)]

    @model_validator(mode="after")
    def _subnets(self) -> Self:
        for net in self.subnets:
            try:
                ipaddress.ip_network(net, strict=False)
            except ValueError as exc:
                raise ValueError(f"discovery.subnets: некорректная подсеть {net!r}") from exc
        return self


class ProbeConfig(Section):
    timeout_s: PositiveFloat
    read_seconds: PositiveFloat
    max_parallel: Annotated[int, Field(gt=0, le=32)]


class AuthConfig(Section):
    session_ttl_hours: PositiveFloat
    password_min_length: Annotated[int, Field(ge=8, le=128)]
    max_failed_logins: Annotated[int, Field(ge=3, le=100)]
    lockout_minutes: PositiveFloat


class SystemAlertsConfig(Section):
    component_down_after_checks: Annotated[int, Field(ge=1, le=20)]
    health_poll_interval_s: PositiveFloat
    doctor_report_max_age_minutes: PositiveFloat


class Defaults(Section):
    version: Literal[1]
    site: SiteConfig
    storage: StorageConfig
    slo: SLOConfig
    camera_health: CameraHealthConfig
    pipeline: PipelineConfig
    routing: RoutingConfig
    vlm: VLMConfig
    incidents: IncidentsConfig
    canary: CanaryConfig
    health: HealthConfig
    doctor: DoctorConfig
    discovery: DiscoveryConfig
    probe: ProbeConfig
    auth: AuthConfig
    system_alerts: SystemAlertsConfig

    @model_validator(mode="after")
    def _offline_detection_slo(self) -> Self:
        # «Тишина ≠ норма»: худший случай обнаружения офлайн-камеры = интервал проверки + таймаут
        # потока. Он обязан укладываться в SLO (SPEC.md §2.2: ≤ 15 с).
        worst = self.camera_health.check_interval_s + self.camera_health.stream_timeout_s
        if worst > self.slo.camera_offline_detection_s:
            raise ValueError(
                f"camera_health: check_interval_s + stream_timeout_s = {worst} с "
                f"превышает SLO обнаружения офлайн-камеры {self.slo.camera_offline_detection_s} с"
            )
        return self


# ---------------------------------------------------------------- scenarios/*.yaml


class _ScenarioBase(Section):
    safety_class: bool
    target_fps: PositiveFloat
    thresholds: ThresholdPair

    # Допустимый диапазон target_fps по SPEC.md §5.4; переопределяется в подклассах.
    fps_range: ClassVar[tuple[float, float]] = (0.1, 30.0)

    @model_validator(mode="after")
    def _fps_in_range(self) -> Self:
        lo, hi = self.fps_range
        if not lo <= self.target_fps <= hi:
            raise ValueError(
                f"target_fps={self.target_fps} вне диапазона {lo}–{hi} из SPEC.md §5.4"
            )
        return self


class FallScenario(_ScenarioBase):
    scenario: Literal[ScenarioKind.FALL]
    lying_seconds: PositiveFloat
    fps_range: ClassVar[tuple[float, float]] = (5.0, 10.0)


class RedZoneScenario(_ScenarioBase):
    scenario: Literal[ScenarioKind.RED_ZONE]
    dwell_seconds: PositiveFloat
    exit_hysteresis_s: PositiveFloat
    fps_range: ClassVar[tuple[float, float]] = (2.0, 10.0)


class SmokeFireScenario(_ScenarioBase):
    scenario: Literal[ScenarioKind.SMOKE_FIRE]
    window_seconds: PositiveFloat
    min_area_growth_ratio: Ratio
    respect_work_schedule: bool
    fps_range: ClassVar[tuple[float, float]] = (2.0, 4.0)


ScenarioConfig = Annotated[
    FallScenario | RedZoneScenario | SmokeFireScenario, Field(discriminator="scenario")
]


# ---------------------------------------------------------------- camera_url_templates.yaml

_TEMPLATE_PLACEHOLDERS = {"host", "port", "channel"}


def _check_template(template: str) -> None:
    names = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    unknown = names - _TEMPLATE_PLACEHOLDERS
    if unknown:
        raise ValueError(f"неизвестные плейсхолдеры: {sorted(unknown)}")
    rendered = template.format(host="192.0.2.10", port=554, channel=1)
    parts = urlsplit(rendered)
    if parts.scheme not in {"rtsp", "rtsps"}:
        raise ValueError(f"шаблон должен начинаться с rtsp://: {template}")
    if parts.username or parts.password:
        raise ValueError("учётные данные в шаблоне запрещены — они подставляются из .secrets/")


class VendorTemplates(Section):
    main: str
    sub: list[str] = Field(min_length=1)
    default_port: Annotated[int, Field(gt=0, lt=65536)]
    verified: bool

    @model_validator(mode="after")
    def _templates(self) -> Self:
        for t in (self.main, *self.sub):
            _check_template(t)
        return self


class CameraUrlTemplates(Section):
    vendors: dict[Identifier, VendorTemplates] = Field(min_length=1)


# ---------------------------------------------------------------- alerts.yaml


class TelegramChannel(Section):
    enabled: bool
    bot_token_secret: Identifier
    timeout_s: PositiveFloat


class RelayHttp(Section):
    url: str | None
    method: Literal["POST", "PUT", "GET"]
    timeout_s: PositiveFloat


class RelayModbus(Section):
    host: str | None
    port: Annotated[int, Field(gt=0, lt=65536)]
    unit_id: Annotated[int, Field(ge=0, le=247)]
    coil: Annotated[int, Field(ge=0)]
    pulse_seconds: PositiveFloat


class LocalRelayChannel(Section):
    enabled: bool
    http: RelayHttp
    modbus: RelayModbus

    @model_validator(mode="after")
    def _target(self) -> Self:
        if self.enabled and not (self.http.url or self.modbus.host):
            raise ValueError("local_relay включён, но не задан ни http.url, ни modbus.host")
        return self


class OpcUaChannel(Section):
    enabled: bool
    endpoint: str | None

    @model_validator(mode="after")
    def _endpoint(self) -> Self:
        if self.enabled and not self.endpoint:
            raise ValueError("opcua включён, но endpoint не задан")
        return self


class WebUiChannel(Section):
    enabled: Literal[True]  # веб-UI — канал по умолчанию, его нельзя отключить (SPEC.md §8.2)


class ChannelsConfig(Section):
    web_ui: WebUiChannel
    telegram: TelegramChannel
    local_relay: LocalRelayChannel
    opcua: OpcUaChannel

    def is_enabled(self, kind: ChannelKind) -> bool:
        return bool(getattr(self, kind.value).enabled)


class DeliveryConfig(Section):
    max_attempts: PositiveInt
    initial_backoff_s: PositiveFloat
    max_backoff_s: PositiveFloat


class RecipientGroup(Section):
    roles: list[Role] = Field(min_length=1)
    zones: list[Identifier] = Field(default_factory=list, description="Пусто — все зоны")
    shifts: list[str] = Field(default_factory=list, description="Пусто — все смены")
    telegram_chat_ids: list[str] = Field(default_factory=list)


class EscalationLevel(Section):
    level: Annotated[int, Field(ge=0, le=10)]
    after_minutes: Annotated[float, Field(ge=0)]
    groups: list[Identifier] = Field(min_length=1)
    channels: list[ChannelKind] = Field(min_length=1)


class SafetyRouting(Section):
    escalation: list[EscalationLevel] = Field(min_length=1)

    @model_validator(mode="after")
    def _monotonic(self) -> Self:
        levels = [e.level for e in self.escalation]
        if levels != list(range(len(levels))):
            raise ValueError("уровни эскалации должны идти подряд с 0")
        delays = [e.after_minutes for e in self.escalation]
        if delays[0] != 0:
            raise ValueError("уровень 0 эскалации отправляется сразу (after_minutes: 0)")
        if any(b <= a for a, b in pairwise(delays)):
            raise ValueError("after_minutes уровней эскалации должны строго возрастать")
        return self


class SystemRouting(Section):
    groups: list[Identifier] = Field(min_length=1)
    channels: list[ChannelKind] = Field(min_length=1)


_SYSTEM_ROLES = {Role.SITE_ADMIN, Role.QOIMA_ENGINEER}


class AlertsConfig(Section):
    channels: ChannelsConfig
    delivery: DeliveryConfig
    groups: dict[Identifier, RecipientGroup] = Field(min_length=1)
    safety: SafetyRouting
    system: SystemRouting

    @model_validator(mode="after")
    def _references(self) -> Self:
        used = {g for lvl in self.safety.escalation for g in lvl.groups} | set(self.system.groups)
        missing = used - set(self.groups)
        if missing:
            raise ValueError(f"неизвестные группы получателей: {sorted(missing)}")
        # Системные алерты никогда не смешиваются с алертами безопасности (SPEC.md §6.1).
        for name in self.system.groups:
            extra = set(self.groups[name].roles) - _SYSTEM_ROLES
            if extra:
                raise ValueError(
                    f"группа {name} получает системные алерты, но содержит роли {sorted(extra)}; "
                    "системные алерты — только для site_admin и qoima_engineer"
                )
        if ChannelKind.LOCAL_RELAY in self.system.channels:
            raise ValueError("сирена/реле не используется для системных алертов")
        return self


# ---------------------------------------------------------------- всё вместе


class QoimaConfig(Contract):
    defaults: Defaults
    scenarios: dict[ScenarioKind, ScenarioConfig]
    camera_url_templates: CameraUrlTemplates
    alerts: AlertsConfig

    @model_validator(mode="after")
    def _cross_file(self) -> Self:
        missing = set(ScenarioKind) - set(self.scenarios)
        if missing:
            raise ValueError(f"нет конфигов сценариев: {sorted(m.value for m in missing)}")
        for kind, sc in self.scenarios.items():
            if sc.scenario is not kind:
                raise ValueError(
                    f"файл сценария {kind.value} содержит scenario={sc.scenario.value}"
                )
            # Сценарии безопасности не могут быть «понижены» конфигом объекта (SPEC.md §2.6).
            if not sc.safety_class:
                raise ValueError(f"сценарий {kind.value} — класс безопасности, safety_class: true")
        shift_names = {s.name for s in self.defaults.site.shifts}
        for name, group in self.alerts.groups.items():
            unknown = set(group.shifts) - shift_names
            if unknown:
                raise ValueError(f"группа {name}: неизвестные смены {sorted(unknown)}")
        return self
