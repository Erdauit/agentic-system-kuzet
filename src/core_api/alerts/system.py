"""Системные алерты (SPEC.md §6.1): камеры и компоненты.

Источники:
- снимки CameraHealth из Valkey Streams (vision-pipeline публикует их каждые ≤ 5 с);
- сторож тишины: нет снимков дольше camera_health.pipeline_silence_s → все камеры «нет данных»
  и алерт по vision-pipeline (тишина ≠ норма, SPEC.md §2.2);
- периодический опрос «Состояния системы»: компонент красный N проверок подряд → алерт.

Антидребезг проблем камер делает vision-pipeline (raise_after_checks/clear_after_checks), здесь
алерт поднимается и снимается по уже подтверждённым проблемам. Системные алерты никогда не
уходят в каналы операторов — только группам из alerts.system (site_admin, qoima_engineer).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Histogram
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from valkey.asyncio import Valkey
from valkey.exceptions import ResponseError

from core_api.alerts.channels import AlertMessage, ChannelDispatcher
from core_api.audit import write_audit
from core_api.db import models
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import (
    CameraHealth,
    CameraHealthStatus,
    CameraIssue,
    HealthStatus,
    SystemAlert,
    SystemAlertKind,
    SystemAlertSeverity,
    SystemHealth,
    system_alert_key,
)
from shared.telemetry import get_logger

log = get_logger("system-alerts")

HEALTH_STREAM = "qoima:camera_health"
CONSUMER_GROUP = "core-api"

ISSUE_ALERTS: dict[CameraIssue, tuple[SystemAlertKind, SystemAlertSeverity]] = {
    CameraIssue.STREAM_DOWN: (SystemAlertKind.CAMERA_DOWN, SystemAlertSeverity.CRITICAL),
    CameraIssue.FROZEN: (SystemAlertKind.CAMERA_FROZEN, SystemAlertSeverity.CRITICAL),
    CameraIssue.BLACK: (SystemAlertKind.CAMERA_BLACK, SystemAlertSeverity.CRITICAL),
    CameraIssue.OVEREXPOSED: (SystemAlertKind.CAMERA_OVEREXPOSED, SystemAlertSeverity.WARNING),
    CameraIssue.BLUR: (SystemAlertKind.CAMERA_BLUR, SystemAlertSeverity.WARNING),
    CameraIssue.OCCLUDED: (SystemAlertKind.CAMERA_OCCLUDED, SystemAlertSeverity.CRITICAL),
    CameraIssue.VIEW_SHIFT: (SystemAlertKind.CAMERA_VIEW_SHIFT, SystemAlertSeverity.WARNING),
}


def camera_message(kind: SystemAlertKind, name: str, camera_id: str, health: CameraHealth) -> str:
    who = f"Камера «{name}» ({camera_id})"
    if kind is SystemAlertKind.CAMERA_DOWN:
        since = ""
        if health.last_packet_at is not None:
            seconds = (health.checked_at - health.last_packet_at).total_seconds()
            since = f" {seconds:.0f} с"
        elif health.reconnects_total == 0:
            since = " — поток ни разу не открылся"
        return f"{who}: нет видеопотока{since}. Проверьте питание, сеть и доступность камеры."
    if kind is SystemAlertKind.CAMERA_FROZEN:
        return f"{who}: изображение замёрзло — кадр не меняется, хотя поток идёт. Перезагрузите камеру."
    if kind is SystemAlertKind.CAMERA_BLACK:
        return f"{who}: чёрный кадр. Проверьте ИК-подсветку, объектив и не закрыта ли камера."
    if kind is SystemAlertKind.CAMERA_OVEREXPOSED:
        return f"{who}: кадр пересвечен. Проверьте экспозицию и прямой свет в объектив."
    if kind is SystemAlertKind.CAMERA_BLUR:
        drop = (
            f" на {100 * (1 - health.sharpness_ratio):.0f}%"
            if health.sharpness_ratio is not None
            else ""
        )
        return f"{who}: расфокус — резкость упала{drop} от базовой. Проверьте объектив (грязь, конденсат, фокус)."
    if kind is SystemAlertKind.CAMERA_OCCLUDED:
        part = (
            f" {100 * health.occluded_ratio:.0f}% кадра"
            if health.occluded_ratio is not None
            else ""
        )
        return f"{who}: камера заслонена{part}. Уберите препятствие перед объективом."
    if kind is SystemAlertKind.CAMERA_VIEW_SHIFT:
        return (
            f"{who}: сдвинулся ракурс — зоны и калибровка больше не совпадают с картинкой. "
            "Верните камеру или заново снимите базовую линию."
        )
    return f"{who}: {kind.value}"


@dataclass
class HealthState:
    """Последние снимки Camera Health (для API и UI) и время последнего снимка от пайплайна."""

    latest: dict[str, CameraHealth] = field(default_factory=dict)
    last_message_at: datetime | None = None
    pipeline_silent: bool = False


class SystemAlertManager:
    def __init__(
        self,
        engine: AsyncEngine,
        valkey: Valkey,
        broadcaster: Broadcaster,
        dispatcher: ChannelDispatcher,
        config: Callable[[], QoimaConfig],
        camera_names: Callable[[], dict[str, str]],
        offline_detection: Histogram | None,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.valkey = valkey
        self.broadcaster = broadcaster
        self.dispatcher = dispatcher
        self.config = config
        self.camera_names = camera_names
        self.offline_detection = offline_detection
        self.state = HealthState()
        self._red_counts: dict[str, int] = {}
        self._started_at = datetime.now(UTC)

    # ------------------------------------------------------------ жизненный цикл алерта

    async def raise_alert(
        self,
        kind: SystemAlertKind,
        severity: SystemAlertSeverity,
        component: str,
        message: str,
        camera_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SystemAlert | None:
        """Поднимает алерт, если такого активного ещё нет. Возвращает новый алерт или None."""
        now = datetime.now(UTC)
        key = system_alert_key(kind, component, camera_id)
        alert_id = uuid.uuid4()
        async with self.sessions() as s:
            stmt = (
                pg_insert(models.SystemAlert)
                .values(
                    id=alert_id,
                    kind=kind.value,
                    severity=severity.value,
                    component=component,
                    camera_id=camera_id,
                    dedup_key=key,
                    message=message,
                    details=details or {},
                    raised_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["dedup_key"],
                    index_where=models.SystemAlert.cleared_at.is_(None),
                )
                .returning(models.SystemAlert.id)
            )
            created = (await s.execute(stmt)).scalar_one_or_none()
            await s.commit()
        if created is None:
            return None
        alert = SystemAlert(
            id=alert_id,
            kind=kind,
            severity=severity,
            component=component,
            camera_id=camera_id,
            message=message,
            details=details or {},
            raised_at=now,
        )
        log.warning("system_alert_raised", kind=kind.value, component=component, camera=camera_id)
        self.broadcaster.publish(
            {"type": "system_alert", "event": "raised", "alert": alert.model_dump(mode="json")}
        )
        await self._deliver(alert)
        return alert

    async def clear_alert(
        self, kind: SystemAlertKind, component: str, camera_id: str | None = None
    ) -> None:
        key = system_alert_key(kind, component, camera_id)
        async with self.sessions() as s:
            row = await s.scalar(
                select(models.SystemAlert).where(
                    models.SystemAlert.dedup_key == key, models.SystemAlert.cleared_at.is_(None)
                )
            )
            if row is None:
                return
            row.cleared_at = datetime.now(UTC)
            await s.commit()
            alert = to_contract(row)
        log.info("system_alert_cleared", kind=kind.value, component=component, camera=camera_id)
        self.broadcaster.publish(
            {"type": "system_alert", "event": "cleared", "alert": alert.model_dump(mode="json")}
        )

    async def acknowledge(self, alert_id: uuid.UUID, actor: str) -> SystemAlert | None:
        async with self.sessions() as s:
            row = await s.get(models.SystemAlert, alert_id)
            if row is None:
                return None
            if row.acknowledged_at is None:
                row.acknowledged_at = datetime.now(UTC)
                row.acknowledged_by = actor
                await write_audit(
                    s, actor=actor, action="system_alert_ack", target=f"system_alert:{alert_id}"
                )
                await s.commit()
            alert = to_contract(row)
        self.broadcaster.publish(
            {
                "type": "system_alert",
                "event": "acknowledged",
                "alert": alert.model_dump(mode="json"),
            }
        )
        return alert

    async def active_alerts(self) -> list[SystemAlert]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(models.SystemAlert)
                .where(models.SystemAlert.cleared_at.is_(None))
                .order_by(models.SystemAlert.raised_at.desc())
            )
            return [to_contract(r) for r in rows]

    async def recent_alerts(self, limit: int = 200) -> list[SystemAlert]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(models.SystemAlert)
                .order_by(models.SystemAlert.raised_at.desc())
                .limit(limit)
            )
            return [to_contract(r) for r in rows]

    async def _deliver(self, alert: SystemAlert) -> None:
        cfg = self.config().alerts
        msg = AlertMessage(
            title=f"Системный алерт: {alert.kind.value}",
            text=alert.message,
            severity=alert.severity.value,
        )
        for channel in cfg.system.channels:
            try:
                results = await self.dispatcher.send(cfg, channel, cfg.system.groups, msg)
            except Exception as exc:  # доставка не должна ронять мониторинг
                log.warning("system_alert_delivery_error", channel=channel.value, error=str(exc))
                continue
            for r in results:
                if not r.ok and cfg.channels.is_enabled(channel):
                    log.warning(
                        "system_alert_delivery_failed", channel=channel.value, error=r.error
                    )

    # ------------------------------------------------------------ Camera Health

    async def handle_health(self, health: CameraHealth) -> None:
        self.state.latest[health.camera_id] = health
        self.state.last_message_at = datetime.now(UTC)
        self.broadcaster.publish(
            {"type": "camera_health", "health": health.model_dump(mode="json")}
        )
        name = self.camera_names().get(health.camera_id, health.camera_id)
        active_issues = set(health.issues)
        for issue, (kind, severity) in ISSUE_ALERTS.items():
            if issue in active_issues:
                details: dict[str, Any] = {"issues": [i.value for i in health.issues]}
                if issue is CameraIssue.STREAM_DOWN and health.last_packet_at is not None:
                    latency = (datetime.now(UTC) - health.last_packet_at).total_seconds()
                    details["detection_seconds"] = round(latency, 2)
                created = await self.raise_alert(
                    kind,
                    severity,
                    "camera-health",
                    camera_message(kind, name, health.camera_id, health),
                    camera_id=health.camera_id,
                    details=details,
                )
                if (
                    created is not None
                    and issue is CameraIssue.STREAM_DOWN
                    and self.offline_detection is not None
                ):
                    seconds = details.get("detection_seconds")
                    if isinstance(seconds, float):
                        self.offline_detection.observe(seconds)
            else:
                await self.clear_alert(kind, "camera-health", health.camera_id)

    async def forget_camera(self, camera_id: str) -> None:
        """Камера отключена или удалена: её алерты снимаются, снимок забывается."""
        self.state.latest.pop(camera_id, None)
        for kind, _severity in ISSUE_ALERTS.values():
            await self.clear_alert(kind, "camera-health", camera_id)

    async def consume(self) -> None:
        """Чтение снимков Camera Health из Valkey Streams (группа потребителей core-api)."""
        with contextlib.suppress(ResponseError):  # BUSYGROUP — группа уже есть
            await self.valkey.xgroup_create(HEALTH_STREAM, CONSUMER_GROUP, id="$", mkstream=True)
        while True:
            try:
                # block (1 с) меньше таймаута сокета клиента (2 с), иначе пустое ожидание — ошибка.
                batches = await self.valkey.xreadgroup(
                    CONSUMER_GROUP, "core-api-1", {HEALTH_STREAM: ">"}, count=200, block=1000
                )
            except Exception as exc:
                log.warning("health_stream_read_failed", error=str(exc))
                await asyncio.sleep(2)
                continue
            for _stream, messages in batches or []:
                for msg_id, fields in messages:
                    raw = fields.get(b"json") or fields.get("json")
                    try:
                        if raw is not None:
                            await self.handle_health(CameraHealth.model_validate_json(raw))
                    except Exception as exc:  # битое сообщение не должно останавливать поток
                        log.warning("health_message_invalid", error=str(exc))
                    await self.valkey.xack(HEALTH_STREAM, CONSUMER_GROUP, msg_id)

    async def watch_silence(self) -> None:
        """Сторож: пайплайн перестал присылать Camera Health → «нет данных» по всем камерам."""
        while True:
            await asyncio.sleep(2)
            cfg = self.config().defaults.camera_health
            if not self.camera_names():
                continue
            last = self.state.last_message_at
            silent_for = (datetime.now(UTC) - last).total_seconds() if last else None
            if last is None or (silent_for is not None and silent_for > cfg.pipeline_silence_s):
                if not self.state.pipeline_silent and (
                    last is not None or self._uptime() > cfg.pipeline_silence_s
                ):
                    self.state.pipeline_silent = True
                    await self.raise_alert(
                        SystemAlertKind.COMPONENT_DOWN,
                        SystemAlertSeverity.CRITICAL,
                        "vision-pipeline",
                        "vision-pipeline не присылает данные о камерах — состояние камер неизвестно. "
                        "Проверьте сервис: qoima status.",
                    )
            elif self.state.pipeline_silent:
                self.state.pipeline_silent = False
                await self.clear_alert(SystemAlertKind.COMPONENT_DOWN, "vision-pipeline")

    def _uptime(self) -> float:
        return (datetime.now(UTC) - self._started_at).total_seconds()

    def camera_status(self, camera_id: str) -> CameraHealth | None:
        """Последний снимок; при молчании пайплайна он устарел — вызывающий показывает UNKNOWN."""
        return self.state.latest.get(camera_id)

    def is_stale(self, health: CameraHealth) -> bool:
        age = (datetime.now(UTC) - health.checked_at).total_seconds()
        return age > self.config().defaults.camera_health.pipeline_silence_s

    # ------------------------------------------------------------ компоненты

    async def handle_system_health(self, health: SystemHealth) -> None:
        threshold = self.config().defaults.system_alerts.component_down_after_checks
        for c in health.components:
            if c.kind.value == "cameras" or c.status is HealthStatus.NOT_DEPLOYED:
                continue
            if c.status is HealthStatus.RED:
                self._red_counts[c.component] = self._red_counts.get(c.component, 0) + 1
                if self._red_counts[c.component] >= threshold:
                    await self.raise_alert(
                        SystemAlertKind.COMPONENT_DOWN,
                        SystemAlertSeverity.CRITICAL,
                        c.component,
                        f"Компонент {c.component}: {c.message}",
                    )
            else:
                if self._red_counts.get(c.component):
                    self._red_counts[c.component] = 0
                await self.clear_alert(SystemAlertKind.COMPONENT_DOWN, c.component)


def to_contract(row: models.SystemAlert) -> SystemAlert:
    return SystemAlert(
        id=row.id,
        kind=SystemAlertKind(row.kind),
        severity=SystemAlertSeverity(row.severity),
        component=row.component,
        camera_id=row.camera_id,
        message=row.message,
        details=row.details,
        raised_at=row.raised_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        cleared_at=row.cleared_at,
    )


def effective_status(health: CameraHealth | None, stale: bool) -> CameraHealthStatus:
    if health is None or stale:
        return CameraHealthStatus.UNKNOWN
    return health.status
