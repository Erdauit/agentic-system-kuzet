"""Инциденты (SPEC.md §7): события из hot path → инцидент с дедупликацией → алерты.

Потоки Valkey (группа потребителей core-api, at-least-once):
- qoima:events — события FSM и служебные события самотеста/канарейки;
- qoima:event_media — клип и ключевые кадры события (через clip_post_s после него).

Повторная доставка того же события безвредна: событие вставляется по id один раз.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey
from valkey.exceptions import ResponseError

from core_api.alerts.delivery import DeliveryService
from core_api.audit import write_audit
from core_api.db import models
from core_api.incidents.policy import SEVERITY, dedup_key, joins
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import (
    SAFETY_EVENT_TYPES,
    ClipRef,
    Event,
    EventMedia,
    EventType,
    FeedbackVerdict,
    FrameRef,
    Incident,
    IncidentState,
    ReasonCode,
    Severity,
    SyntheticKind,
)
from shared.telemetry import get_logger

log = get_logger("incidents")

EVENTS_STREAM = "qoima:events"
MEDIA_STREAM = "qoima:event_media"
CONSUMER_GROUP = "core-api"
CONSUMER = "core-api-1"
# Сколько раз пробовать сообщение, которое падает с ошибкой, прежде чем отложить его.
MAX_ATTEMPTS = 5
DEAD_LETTER_MAXLEN = 10_000


class IncidentError(Exception):
    """Недопустимое действие с инцидентом (не найден, уже закрыт и т.п.)."""


@dataclass(frozen=True)
class IncidentView:
    incident: Incident
    events: list[Event]


def event_to_contract(row: models.Event) -> Event:
    return Event(
        id=row.id,
        type=EventType(row.type),
        camera_id=row.camera_id,
        zone_id=row.zone_id,
        track_ids=row.track_ids,
        started_at=row.started_at,
        detected_at=row.detected_at,
        detector_score=row.detector_score,
        routing=row.routing,  # type: ignore[arg-type]
        model_version=row.model_version,
        evidence=[FrameRef.model_validate(f) for f in row.evidence],
        area=row.area,
        synthetic=SyntheticKind(row.synthetic) if row.synthetic else None,
    )


def incident_to_contract(row: models.Incident, event_ids: list[uuid.UUID]) -> Incident:
    return Incident(
        id=row.id,
        type=EventType(row.type),
        state=IncidentState(row.state),
        severity=Severity(row.severity),
        zone_id=row.zone_id,
        camera_ids=row.camera_ids,
        event_ids=event_ids,
        dedup_key=row.dedup_key,
        opened_at=row.opened_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        clip=ClipRef.model_validate(row.clip) if row.clip else None,
        keyframes=[FrameRef.model_validate(f) for f in row.keyframes][:6],
        area=row.area,
        synthetic=SyntheticKind(row.synthetic) if row.synthetic else None,
        escalation_level=row.escalation_level,
    )


class IncidentManager:
    def __init__(
        self,
        engine: AsyncEngine,
        valkey: Valkey,
        broadcaster: Broadcaster,
        delivery: DeliveryService,
        config: Callable[[], QoimaConfig],
        alerts_total: Counter,
        ack_seconds: Histogram,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.valkey = valkey
        self.broadcaster = broadcaster
        self.delivery = delivery
        self.config = config
        self.alerts_total = alerts_total
        self.ack_seconds = ack_seconds

    # ------------------------------------------------------------ события

    async def handle_event(self, event: Event) -> uuid.UUID | None:
        """Сохраняет событие и относит его к инциденту. Возвращает id инцидента (или None)."""
        now = datetime.now(UTC)
        cfg = self.config().defaults.incidents
        async with self.sessions() as s:
            if await s.get(models.Event, event.id) is not None:
                return None  # повтор из потока
            if await s.get(models.Camera, event.camera_id) is None:
                log.warning("event_for_unknown_camera", camera=event.camera_id)
                return None
            zone_id = event.zone_id
            if zone_id is not None and await s.get(models.Zone, zone_id) is None:
                zone_id = None
            row = models.Event(
                id=event.id,
                type=event.type.value,
                camera_id=event.camera_id,
                zone_id=zone_id,
                track_ids=event.track_ids,
                started_at=event.started_at,
                detected_at=event.detected_at,
                detector_score=event.detector_score,
                routing=event.routing.value,
                model_version=event.model_version,
                evidence=[f.model_dump(mode="json") for f in event.evidence],
                area=event.area,
                synthetic=event.synthetic.value if event.synthetic else None,
            )
            if event.type not in SAFETY_EVENT_TYPES:
                # FALL_RECOVERED и т.п.: микро-инцидент — запись в ленте событий, без алерта.
                s.add(row)
                await s.commit()
                self.broadcaster.publish({"type": "event", "event": event.model_dump(mode="json")})
                return None
            key = dedup_key(event)
            incident = await s.scalar(
                select(models.Incident)
                .where(
                    models.Incident.dedup_key == key,
                    models.Incident.state.in_(
                        [IncidentState.OPEN.value, IncidentState.ACKNOWLEDGED.value]
                    ),
                )
                .order_by(models.Incident.opened_at.desc())
                .limit(1)
                .with_for_update()
            )
            created = incident is None or not joins(
                IncidentState(incident.state),
                incident.opened_at,
                incident.last_event_at,
                event.detected_at,
                cfg,
            )
            if created or incident is None:
                incident = models.Incident(
                    id=uuid.uuid4(),
                    type=event.type.value,
                    state=IncidentState.OPEN.value,
                    severity=SEVERITY[event.type].value,
                    zone_id=zone_id,
                    camera_ids=[event.camera_id],
                    dedup_key=key,
                    opened_at=now,
                    area=event.area,
                    synthetic=event.synthetic.value if event.synthetic else None,
                    escalation_level=0,
                    last_event_at=event.detected_at,
                )
                s.add(incident)
                await s.flush()
            else:
                if event.camera_id not in incident.camera_ids:
                    incident.camera_ids = [*incident.camera_ids, event.camera_id]
                incident.last_event_at = max(incident.last_event_at or now, event.detected_at)
            row.incident_id = incident.id
            s.add(row)
            await s.commit()
            view = await self._view(s, incident)
        log.info(
            "incident_opened" if created else "incident_event_joined",
            incident=str(view.incident.id),
            type=event.type.value,
            camera=event.camera_id,
            key=key,
        )
        self.broadcaster.publish(
            {
                "type": "incident",
                "event": "opened" if created else "updated",
                "new_event": True,
                "incident": view.incident.model_dump(mode="json"),
            }
        )
        if created:
            if event.synthetic is None:
                self.alerts_total.labels(severity=view.incident.severity.value).inc()
            await self.delivery.enqueue_incident(view.incident.id, 0)
        return view.incident.id

    async def handle_media(self, media: EventMedia) -> None:
        async with self.sessions() as s:
            ev = await s.get(models.Event, media.event_id)
            if ev is None or ev.incident_id is None:
                if media.error:
                    log.warning(
                        "event_media_error", event_id=str(media.event_id), error=media.error
                    )
                return
            incident = await s.get(models.Incident, ev.incident_id, with_for_update=True)
            if incident is None:
                return
            new_clip = incident.clip is None and media.clip is not None
            if new_clip and media.clip is not None:
                incident.clip = media.clip.model_dump(mode="json")
                incident.keyframes = [k.model_dump(mode="json") for k in media.keyframes]
            await s.commit()
            view = await self._view(s, incident)
        if media.error:
            log.warning("event_media_error", event_id=str(media.event_id), error=media.error)
        self.broadcaster.publish(
            {
                "type": "incident",
                "event": "updated",
                "incident": view.incident.model_dump(mode="json"),
                "media_error": media.error,
            }
        )
        if new_clip:
            await self.delivery.enqueue_clip(view.incident.id)

    # ------------------------------------------------------------ чтение

    async def _view(self, s: AsyncSession, row: models.Incident) -> IncidentView:
        events = list(
            await s.scalars(
                select(models.Event)
                .where(models.Event.incident_id == row.id)
                .order_by(models.Event.detected_at)
            )
        )
        return IncidentView(
            incident_to_contract(row, [e.id for e in events]),
            [event_to_contract(e) for e in events],
        )

    async def get(self, incident_id: uuid.UUID) -> IncidentView | None:
        async with self.sessions() as s:
            row = await s.get(models.Incident, incident_id)
            return await self._view(s, row) if row is not None else None

    async def list_incidents(
        self,
        *,
        states: list[IncidentState] | None = None,
        include_synthetic: bool = False,
        limit: int = 100,
    ) -> list[IncidentView]:
        async with self.sessions() as s:
            q = select(models.Incident).order_by(models.Incident.opened_at.desc()).limit(limit)
            if states:
                q = q.where(models.Incident.state.in_([st.value for st in states]))
            if not include_synthetic:
                q = q.where(models.Incident.synthetic.is_(None))
            rows = list(await s.scalars(q))
            return [await self._view(s, r) for r in rows]

    async def recent_events(self, limit: int = 100) -> list[Event]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(models.Event)
                .where(models.Event.synthetic.is_(None))
                .order_by(models.Event.detected_at.desc())
                .limit(limit)
            )
            return [event_to_contract(r) for r in rows]

    async def open_count(self) -> int:
        async with self.sessions() as s:
            return int(
                await s.scalar(
                    select(func.count())
                    .select_from(models.Incident)
                    .where(
                        models.Incident.state == IncidentState.OPEN.value,
                        models.Incident.synthetic.is_(None),
                    )
                )
                or 0
            )

    # ------------------------------------------------------------ действия оператора

    async def acknowledge(self, incident_id: uuid.UUID, actor: str) -> Incident:
        now = datetime.now(UTC)
        async with self.sessions() as s:
            row = await s.get(models.Incident, incident_id, with_for_update=True)
            if row is None:
                raise IncidentError("инцидент не найден")
            if row.state != IncidentState.OPEN.value:
                raise IncidentError("инцидент уже подтверждён или закрыт")
            self._ack(row, actor, now)
            await write_audit(s, actor=actor, action="incident_ack", target=f"incident:{row.id}")
            await s.commit()
            view = await self._view(s, row)
        await self._after_ack(view.incident)
        return view.incident

    def _ack(self, row: models.Incident, actor: str, now: datetime) -> None:
        row.state = IncidentState.ACKNOWLEDGED.value
        row.acknowledged_at = now
        row.acknowledged_by = actor
        if row.synthetic is None:
            self.ack_seconds.labels(severity=row.severity).observe(
                (now - row.opened_at).total_seconds()
            )

    async def _after_ack(self, incident: Incident) -> None:
        self.broadcaster.publish(
            {
                "type": "incident",
                "event": "acknowledged",
                "incident": incident.model_dump(mode="json"),
            }
        )
        if incident.escalation_level > 0 or incident.synthetic is SyntheticKind.SELFTEST:
            await self.delivery.siren_off()

    async def close_synthetic(self, kind: SyntheticKind, actor: str) -> int:
        """Закрыть незавершённые служебные инциденты (новый самотест не должен сливаться со
        старым). Без отзыва оператора: это не оценка тревоги, а уборка тестовых данных."""
        now = datetime.now(UTC)
        async with self.sessions() as s:
            rows = list(
                await s.scalars(
                    select(models.Incident)
                    .where(
                        models.Incident.synthetic == kind.value,
                        models.Incident.state.in_(
                            [IncidentState.OPEN.value, IncidentState.ACKNOWLEDGED.value]
                        ),
                    )
                    .with_for_update()
                )
            )
            for row in rows:
                if row.state == IncidentState.OPEN.value:
                    row.acknowledged_at, row.acknowledged_by = now, actor
                row.state = IncidentState.RESOLVED.value
                row.resolved_at = now
            if rows:
                await write_audit(
                    s,
                    actor=actor,
                    action="synthetic_incidents_closed",
                    target=kind.value,
                    details={"incidents": [str(r.id) for r in rows]},
                )
            await s.commit()
        return len(rows)

    async def resolve(
        self,
        incident_id: uuid.UUID,
        actor: str,
        verdict: FeedbackVerdict,
        reason: ReasonCode | None,
        comment: str | None,
    ) -> Incident:
        """Закрыть инцидент с отзывом оператора. Ложное срабатывание — только с кодом причины.

        Комментарий — данные для разбора, а не инструкции (SPEC.md §2.6). Из OPEN закрытие
        подтверждает инцидент тем же действием (одна кнопка в карточке).
        """
        if verdict is FeedbackVerdict.FALSE_ALARM and reason is None:
            raise IncidentError("для ложного срабатывания выберите причину")
        now = datetime.now(UTC)
        acked_now = False
        async with self.sessions() as s:
            row = await s.get(models.Incident, incident_id, with_for_update=True)
            if row is None:
                raise IncidentError("инцидент не найден")
            if row.state in (IncidentState.RESOLVED.value, IncidentState.FALSE_ALARM.value):
                raise IncidentError("инцидент уже закрыт")
            if row.state == IncidentState.OPEN.value:
                self._ack(row, actor, now)
                acked_now = True
            row.state = (
                IncidentState.FALSE_ALARM.value
                if verdict is FeedbackVerdict.FALSE_ALARM
                else IncidentState.RESOLVED.value
            )
            row.resolved_at = now
            s.add(
                models.Feedback(
                    id=uuid.uuid4(),
                    incident_id=row.id,
                    verdict=verdict.value,
                    reason_code=reason.value if reason else None,
                    comment=comment,
                    author_id=actor,
                )
            )
            details: dict[str, Any] = {"verdict": verdict.value}
            if reason:
                details["reason_code"] = reason.value
            await write_audit(
                s,
                actor=actor,
                action="incident_resolve",
                target=f"incident:{row.id}",
                details=details,
            )
            await s.commit()
            view = await self._view(s, row)
        if acked_now:
            await self._after_ack(view.incident)
        self.broadcaster.publish(
            {
                "type": "incident",
                "event": "resolved",
                "incident": view.incident.model_dump(mode="json"),
            }
        )
        return view.incident

    # ------------------------------------------------------------ потоки Valkey

    async def _consume(
        self, stream: str, handle: Callable[[bytes | str], Any], stop: asyncio.Event
    ) -> None:
        with contextlib.suppress(ResponseError):  # BUSYGROUP — группа уже есть
            await self.valkey.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
        # Сначала — свои неподтверждённые сообщения (core-api упал посреди обработки).
        cursor = "0"
        failures: dict[Any, int] = {}
        while not stop.is_set():
            try:
                batches = await self.valkey.xreadgroup(
                    CONSUMER_GROUP, CONSUMER, {stream: cursor}, count=100, block=1000
                )
            except Exception as exc:
                log.warning("stream_read_failed", stream=stream, error=str(exc))
                await asyncio.sleep(2)
                continue
            got = False
            for _stream, messages in batches or []:
                for msg_id, fields in messages:
                    got = True
                    raw = fields.get(b"json") or fields.get("json")
                    try:
                        if raw is not None:
                            await handle(raw)
                    except ValidationError as exc:  # битое сообщение повтором не исправить
                        log.warning("stream_message_invalid", stream=stream, error=str(exc))
                    except Exception:
                        # Ошибка БД и т.п.: сообщение не подтверждаем — придёт снова. Но ошибка
                        # в самом сообщении не должна навсегда остановить поток: после
                        # MAX_ATTEMPTS попыток оно уходит в «мёртвые письма» для разбора.
                        failures[msg_id] = failures.get(msg_id, 0) + 1
                        log.exception(
                            "stream_message_failed", stream=stream, attempt=failures[msg_id]
                        )
                        if failures[msg_id] < MAX_ATTEMPTS:
                            await asyncio.sleep(1)
                            break
                        await self._dead_letter(stream, msg_id, raw)
                    failures.pop(msg_id, None)
                    await self.valkey.xack(stream, CONSUMER_GROUP, msg_id)
            if cursor == "0" and not got:
                cursor = ">"  # хвост разобран — читаем новые

    async def _dead_letter(self, stream: str, msg_id: Any, raw: bytes | str | None) -> None:
        log.error("stream_message_dead_lettered", stream=stream, message_id=str(msg_id))
        with contextlib.suppress(Exception):
            await self.valkey.xadd(
                f"{stream}:dead",
                {
                    "json": raw.decode() if isinstance(raw, bytes) else (raw or ""),
                    "source_id": str(msg_id),
                },
                maxlen=DEAD_LETTER_MAXLEN,
                approximate=True,
            )

    async def run(self, stop: asyncio.Event) -> None:
        async def on_event(raw: bytes | str) -> None:
            await self.handle_event(Event.model_validate_json(raw))

        async def on_media(raw: bytes | str) -> None:
            await self.handle_media(EventMedia.model_validate_json(raw))

        await asyncio.gather(
            self._consume(EVENTS_STREAM, on_event, stop),
            self._consume(MEDIA_STREAM, on_media, stop),
        )
