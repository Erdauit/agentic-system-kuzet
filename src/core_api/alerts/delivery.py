"""Очередь доставок алертов безопасности (SPEC.md §7): at-least-once, идемпотентный ключ,
повторы с экспоненциальной паузой по каждому каналу отдельно, эскалация.

Доставка — строка alert_deliveries. Обработчик «арендует» созревшие строки (сдвигает
next_attempt_at на LEASE_S и фиксирует транзакцию), отправляет без открытой транзакции и
записывает результат. Упал посреди отправки — аренда истечёт, доставка повторится (получатель
может увидеть сообщение дважды, но не пропустит его). Каналы отправляются параллельно: медленный
или недоступный Telegram не задерживает веб-UI и сирену.

После max_attempts неудач доставка помечается failed и поднимается системный алерт инженерам.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from prometheus_client import Counter, Histogram
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core_api.alerts.channels import AlertMessage, ChannelDispatcher
from core_api.alerts.system import SystemAlertManager
from core_api.db import models
from core_api.incidents.policy import alert_text, routes
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import (
    AlertStream,
    ChannelKind,
    DeliveryStatus,
    EventType,
    IncidentState,
    RoutingDecision,
    SyntheticKind,
    SystemAlertKind,
    SystemAlertSeverity,
    idempotency_key,
)
from shared.storage import LocalFSBlobStore
from shared.telemetry import get_logger

log = get_logger("alert-delivery")

LEASE_S = 60.0
BATCH = 50
CLIP_SUFFIX = "#clip"


def backoff_s(attempt: int, initial: float, maximum: float) -> float:
    """Пауза перед попыткой attempt+1: 1 → 2 → 4 … ≤ maximum."""
    return float(min(initial * (2 ** max(attempt - 1, 0)), maximum))


class DeliveryService:
    def __init__(
        self,
        engine: AsyncEngine,
        dispatcher: ChannelDispatcher,
        broadcaster: Broadcaster,
        config: Callable[[], QoimaConfig],
        blobs: LocalFSBlobStore,
        system_alerts: SystemAlertManager,
        camera_names: Callable[[], dict[str, str]],
        latency: Histogram,
        escalations: Counter,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.dispatcher = dispatcher
        self.broadcaster = broadcaster
        self.config = config
        self.blobs = blobs
        self.system_alerts = system_alerts
        self.camera_names = camera_names
        self.latency = latency
        self.escalations = escalations
        self.wake = asyncio.Event()

    # ------------------------------------------------------------ постановка в очередь

    async def enqueue_incident(self, incident_id: uuid.UUID, level: int) -> int:
        cfg = self.config().alerts
        async with self.sessions() as s:
            inc = await s.get(models.Incident, incident_id)
            if inc is None:
                return 0
            synthetic = SyntheticKind(inc.synthetic) if inc.synthetic else None
            values: list[dict[str, Any]] = []
            for route in routes(cfg, level, synthetic):
                targets = self.dispatcher.targets(
                    cfg, route.channel, route.groups, route.web_target
                )
                if not targets and cfg.channels.is_enabled(route.channel):
                    log.warning(
                        "no_recipients",
                        channel=route.channel.value,
                        groups=route.groups,
                        level=level,
                    )
                for target in targets:
                    values.append(self._row(incident_id, route.channel, target, level))
            await self._insert(s, values)
        self.wake.set()
        return len(values)

    async def enqueue_clip(self, incident_id: uuid.UUID) -> int:
        """Клип — отдельным сообщением в Telegram тем, кому ушёл первый алерт (веб-UI получает
        обновление карточки по WebSocket)."""
        async with self.sessions() as s:
            sent_to = await s.scalars(
                select(models.AlertDeliveryRow.target).where(
                    models.AlertDeliveryRow.incident_id == incident_id,
                    models.AlertDeliveryRow.channel == ChannelKind.TELEGRAM.value,
                    models.AlertDeliveryRow.escalation_level == 0,
                    models.AlertDeliveryRow.target.not_like(f"%{CLIP_SUFFIX}"),
                )
            )
            values = [
                self._row(incident_id, ChannelKind.TELEGRAM, f"{t}{CLIP_SUFFIX}", 0)
                for t in sent_to
            ]
            await self._insert(s, values)
        self.wake.set()
        return len(values)

    @staticmethod
    def _row(
        incident_id: uuid.UUID, channel: ChannelKind, target: str, level: int
    ) -> dict[str, Any]:
        return {
            "idempotency_key": idempotency_key(
                AlertStream.SAFETY, incident_id, channel, target, level
            ),
            "stream": AlertStream.SAFETY.value,
            "channel": channel.value,
            "target": target,
            "incident_id": incident_id,
            "escalation_level": level,
            "attempt": 0,
            "status": DeliveryStatus.PENDING.value,
        }

    @staticmethod
    async def _insert(s: Any, values: list[dict[str, Any]]) -> None:
        if values:
            await s.execute(
                pg_insert(models.AlertDeliveryRow).values(values).on_conflict_do_nothing()
            )
        await s.commit()

    # ------------------------------------------------------------ отправка

    async def process_due(self) -> int:
        now = datetime.now(UTC)
        async with self.sessions() as s:
            rows = list(
                await s.scalars(
                    select(models.AlertDeliveryRow)
                    .where(
                        models.AlertDeliveryRow.stream == AlertStream.SAFETY.value,
                        models.AlertDeliveryRow.status.in_(
                            [DeliveryStatus.PENDING.value, DeliveryStatus.RETRYING.value]
                        ),
                        models.AlertDeliveryRow.next_attempt_at <= now,
                    )
                    .order_by(models.AlertDeliveryRow.created_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            for r in rows:
                r.next_attempt_at = now + timedelta(seconds=LEASE_S)
            await s.commit()
        if rows:
            await asyncio.gather(*(self._attempt(r) for r in rows))
        return len(rows)

    async def _attempt(self, d: models.AlertDeliveryRow) -> None:
        cfg = self.config()
        channel = ChannelKind(d.channel)
        if d.incident_id is None:
            return
        try:
            msg = await self._message(
                d.incident_id, d.escalation_level, d.target.endswith(CLIP_SUFFIX)
            )
            result = await self.dispatcher.send_one(cfg.alerts, channel, d.target, msg)
            ok, error = result.ok, result.error
        except Exception as exc:  # ошибка подготовки сообщения — тоже неудачная попытка
            log.exception("delivery_attempt_crashed", channel=channel.value)
            ok, error = False, f"{type(exc).__name__}: {exc}"
        now = datetime.now(UTC)
        dcfg = cfg.alerts.delivery
        first_sent = False
        failed = False
        async with self.sessions() as s:
            row = await s.get(models.AlertDeliveryRow, d.idempotency_key, with_for_update=True)
            if row is None:
                return
            row.attempt += 1
            if ok:
                row.status, row.sent_at, row.last_error = DeliveryStatus.SENT.value, now, None
                others = await s.scalar(
                    select(models.AlertDeliveryRow.idempotency_key)
                    .where(
                        models.AlertDeliveryRow.incident_id == d.incident_id,
                        models.AlertDeliveryRow.status == DeliveryStatus.SENT.value,
                        models.AlertDeliveryRow.idempotency_key != d.idempotency_key,
                    )
                    .limit(1)
                )
                first_sent = others is None
            elif row.attempt >= dcfg.max_attempts:
                row.status, row.last_error = DeliveryStatus.FAILED.value, error
                failed = True
            else:
                row.status, row.last_error = DeliveryStatus.RETRYING.value, error
                row.next_attempt_at = now + timedelta(
                    seconds=backoff_s(row.attempt, dcfg.initial_backoff_s, dcfg.max_backoff_s)
                )
            attempt = row.attempt
            await s.commit()
        log.info(
            "delivery_attempt",
            channel=channel.value,
            target=d.target,
            level=d.escalation_level,
            attempt=attempt,
            ok=ok,
            error=error,
        )
        self.broadcaster.publish(
            {
                "type": "delivery",
                "incident_id": str(d.incident_id),
                "channel": channel.value,
                "target": d.target,
                "status": "sent" if ok else ("failed" if failed else "retrying"),
                "error": error,
            }
        )
        if ok:
            if first_sent:
                await self._observe_latency(d.incident_id, now)
            await self.system_alerts.clear_alert(
                SystemAlertKind.ALERT_DELIVERY_FAILED, f"channel:{channel.value}"
            )
        elif failed:
            await self.system_alerts.raise_alert(
                SystemAlertKind.ALERT_DELIVERY_FAILED,
                SystemAlertSeverity.CRITICAL,
                f"channel:{channel.value}",
                f"Алерт безопасности не доставлен в канал {channel.value} ({d.target}) после "
                f"{attempt} попыток: {error}. Остальные каналы работают; проверьте канал.",
                details={"incident_id": str(d.incident_id), "target": d.target},
            )

    async def _observe_latency(self, incident_id: uuid.UUID, sent_at: datetime) -> None:
        """SLO: от срабатывания FSM (detected_at первого события) до первого отправленного алерта."""
        async with self.sessions() as s:
            inc = await s.get(models.Incident, incident_id)
            first = await s.scalar(
                select(models.Event)
                .where(models.Event.incident_id == incident_id)
                .order_by(models.Event.detected_at)
                .limit(1)
            )
        if inc is None or first is None or inc.synthetic is not None:
            return
        self.latency.labels(type=inc.type).observe(
            max(0.0, (sent_at - first.detected_at).total_seconds())
        )

    async def _message(self, incident_id: uuid.UUID, level: int, clip: bool) -> AlertMessage:
        cfg = self.config()
        async with self.sessions() as s:
            inc = await s.get(models.Incident, incident_id)
            if inc is None:
                raise LookupError("инцидент удалён")
            first = await s.scalar(
                select(models.Event)
                .where(models.Event.incident_id == incident_id)
                .order_by(models.Event.detected_at)
                .limit(1)
            )
            zone = await s.get(models.Zone, inc.zone_id) if inc.zone_id else None
        site = cfg.defaults.site
        camera_id = first.camera_id if first else inc.camera_ids[0]
        at = (first.detected_at if first else inc.opened_at).astimezone(ZoneInfo(site.timezone))
        text = alert_text(
            site.default_language,
            EventType(inc.type),
            self.camera_names().get(camera_id, camera_id),
            zone.name if zone else None,
            at.strftime("%H:%M:%S"),
            RoutingDecision(first.routing) if first else RoutingDecision.EVENT,
            SyntheticKind(inc.synthetic) if inc.synthetic else None,
            level,
            clip=clip,
        )
        photo = video = None
        if clip and inc.clip:
            video = await asyncio.to_thread(self.blobs.get, inc.clip["blob_key"])
        elif first and first.evidence:
            with contextlib.suppress(KeyError, OSError):
                photo = await asyncio.to_thread(self.blobs.get, first.evidence[0]["blob_key"])
        return AlertMessage(
            title=text.title,
            text=text.text,
            severity=inc.severity,
            photo=photo,
            video=video,
            payload={
                "incident_id": str(inc.id),
                "event_type": inc.type,
                "camera_id": camera_id,
                "zone_id": inc.zone_id,
                "area": inc.area,
                "level": level,
                "synthetic": inc.synthetic,
            },
        )

    def web_hook(self, target: str, msg: AlertMessage) -> int:
        """Доставка в веб-UI: сообщение в WebSocket (звук и баннер в «Оперативной обстановке»)."""
        return self.broadcaster.publish(
            {
                "type": "canary_alert" if target == "system" else "alert",
                "title": msg.title,
                "text": msg.text,
                "severity": msg.severity,
                "sound": target != "system",
                **msg.payload,
            }
        )

    async def siren_off(self) -> None:
        await self.dispatcher.siren_off(self.config().alerts)

    # ------------------------------------------------------------ эскалация

    async def escalate(self) -> int:
        """OPEN-инцидент без подтверждения дольше after_minutes уровня → следующий уровень."""
        levels = self.config().alerts.safety.escalation
        now = datetime.now(UTC)
        reached: list[tuple[uuid.UUID, int, int]] = []
        async with self.sessions() as s:
            rows = await s.scalars(
                select(models.Incident)
                .where(
                    models.Incident.state == IncidentState.OPEN.value,
                    models.Incident.synthetic.is_(None),
                    models.Incident.escalation_level < len(levels) - 1,
                )
                .with_for_update(skip_locked=True)
            )
            for inc in rows:
                old = lvl = inc.escalation_level
                age_min = (now - inc.opened_at).total_seconds() / 60
                while lvl + 1 < len(levels) and age_min >= levels[lvl + 1].after_minutes:
                    lvl += 1
                if lvl > old:
                    inc.escalation_level = lvl
                    inc.last_escalated_at = now
                    reached.append((inc.id, old, lvl))
            await s.commit()
        for incident_id, old, new in reached:
            for level in range(old + 1, new + 1):
                await self.enqueue_incident(incident_id, level)
                self.escalations.labels(level=str(level)).inc()
            log.warning("incident_escalated", incident=str(incident_id), level=new)
            self.broadcaster.publish(
                {
                    "type": "incident",
                    "event": "escalated",
                    "incident_id": str(incident_id),
                    "level": new,
                }
            )
        return len(reached)

    async def deliveries(self, incident_id: uuid.UUID) -> list[models.AlertDeliveryRow]:
        async with self.sessions() as s:
            return list(
                await s.scalars(
                    select(models.AlertDeliveryRow)
                    .where(models.AlertDeliveryRow.incident_id == incident_id)
                    .order_by(models.AlertDeliveryRow.created_at)
                )
            )

    async def run(self, stop: asyncio.Event) -> None:
        # Аренды, оставшиеся от прошлого запуска, не ждут истечения: доставки сразу снова в работе.
        with contextlib.suppress(Exception):
            async with self.sessions() as s:
                await s.execute(
                    update(models.AlertDeliveryRow)
                    .where(
                        models.AlertDeliveryRow.status.in_(
                            [DeliveryStatus.PENDING.value, DeliveryStatus.RETRYING.value]
                        ),
                        models.AlertDeliveryRow.attempt == 0,
                    )
                    .values(next_attempt_at=datetime.now(UTC))
                )
                await s.commit()
        while not stop.is_set():
            self.wake.clear()
            try:
                while await self.process_due() == BATCH:
                    pass
                await self.escalate()
            except Exception as exc:  # БД недоступна — повторим; события лежат в потоке
                log.warning("delivery_loop_failed", error=str(exc))
            interval = self.config().alerts.delivery.poll_interval_s
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=interval)
