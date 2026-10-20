"""Сквозной самотест (мастер, шаг 8) и канарейка пути алерта (SPEC.md §3.2, §6.1).

Обе проверки просят vision-pipeline создать служебное событие на настоящей камере: кадр и клип
берутся из её GOP-буфера, событие идёт обычным путём (поток событий → инцидент → доставки).

- Самотест: событие на выбранных камерах; камеры одного участка дают один инцидент (проверка
  дедупликации). Алерт уходит во все каналы с пометкой «САМОТЕСТ»; человек подтверждает
  получение — инциденты самотеста закрываются.
- Канарейка: раз в canary.interval_minutes событие на одной камере; ожидается доставка в
  системную ленту веб-UI за canary.timeout_s. Нет — системный алерт canary_failed.

Канарейка с прогоном тестового ролика через модели (ловит деградацию детектора) требует
samples/e2e/fall_staged.mp4 и весов моделей — docs/TODO.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from valkey.asyncio import Valkey

from core_api.alerts.system import SystemAlertManager
from core_api.audit import write_audit
from core_api.db import models
from core_api.incidents.manager import IncidentManager
from shared.config import QoimaConfig
from shared.schemas import (
    DeliveryStatus,
    EventType,
    FeedbackVerdict,
    PipelineCommand,
    SyntheticKind,
    SystemAlertKind,
    SystemAlertSeverity,
)
from shared.telemetry import get_logger

log = get_logger("selftest")

COMMANDS_STREAM = "qoima:pipeline:commands"
COMMANDS_MAXLEN = 1000
RUNS_KEPT = 20


@dataclass
class SelfTestRun:
    id: uuid.UUID
    started_at: datetime
    started_by: str
    events: dict[str, uuid.UUID]  # камера → id события
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    notes: list[str] = field(default_factory=list)


async def send_command(valkey: Valkey, cmd: PipelineCommand) -> None:
    await valkey.xadd(
        COMMANDS_STREAM, {"json": cmd.model_dump_json()}, maxlen=COMMANDS_MAXLEN, approximate=True
    )


class SelfTestService:
    def __init__(
        self,
        engine: AsyncEngine,
        valkey: Valkey,
        incidents: IncidentManager,
        system_alerts: SystemAlertManager,
        config: Callable[[], QoimaConfig],
        canary_seconds: Histogram,
        canary_failures: Counter,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.valkey = valkey
        self.incidents = incidents
        self.system_alerts = system_alerts
        self.config = config
        self.canary_seconds = canary_seconds
        self.canary_failures = canary_failures
        self.runs: dict[uuid.UUID, SelfTestRun] = {}
        self.last_canary: dict[str, Any] | None = None

    # ------------------------------------------------------------ самотест

    async def start(self, camera_ids: list[str], actor: str) -> SelfTestRun:
        stale = await self.incidents.close_synthetic(SyntheticKind.SELFTEST, "system:selftest")
        if stale:
            log.info("selftest_stale_closed", incidents=stale)
        async with self.sessions() as s:
            cams = list(
                await s.scalars(
                    select(models.Camera).where(
                        models.Camera.id.in_(camera_ids), models.Camera.enabled.is_(True)
                    )
                )
            )
            zones = list(
                await s.scalars(select(models.Zone).where(models.Zone.camera_id.in_(camera_ids)))
            )
            if not cams:
                raise ValueError("нет включённых камер для самотеста")
            run = SelfTestRun(uuid.uuid4(), datetime.now(UTC), actor, {})
            for cam in cams:
                # Зона с участком — чтобы камеры одного участка проверили дедупликацию.
                zone = next(
                    (z for z in zones if z.camera_id == cam.id and z.area),
                    next((z for z in zones if z.camera_id == cam.id), None),
                )
                event_id = uuid.uuid4()
                await send_command(
                    self.valkey,
                    PipelineCommand(
                        event_id=event_id,
                        kind="inject_event",
                        camera_id=cam.id,
                        event_type=EventType.FALL,
                        synthetic=SyntheticKind.SELFTEST,
                        zone_id=zone.id if zone else None,
                        issued_at=run.started_at,
                    ),
                )
                run.events[cam.id] = event_id
            await write_audit(
                s,
                actor=actor,
                action="selftest_start",
                target=f"selftest:{run.id}",
                details={"cameras": sorted(run.events)},
            )
            await s.commit()
        self.runs[run.id] = run
        while len(self.runs) > RUNS_KEPT:
            self.runs.pop(next(iter(self.runs)))
        log.info("selftest_started", run=str(run.id), cameras=sorted(run.events))
        return run

    async def status(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        slo = self.config().defaults.slo.fall_to_alert_p95_s
        async with self.sessions() as s:
            events = {
                e.id: e
                for e in await s.scalars(
                    select(models.Event).where(models.Event.id.in_(list(run.events.values())))
                )
            }
            incident_ids = sorted(
                {e.incident_id for e in events.values() if e.incident_id}, key=str
            )
            incidents = {
                i.id: i
                for i in await s.scalars(
                    select(models.Incident).where(models.Incident.id.in_(incident_ids))
                )
            }
            deliveries = list(
                await s.scalars(
                    select(models.AlertDeliveryRow).where(
                        models.AlertDeliveryRow.incident_id.in_(incident_ids)
                    )
                )
            )
        cams = []
        for camera_id, event_id in run.events.items():
            ev = events.get(event_id)
            cams.append(
                {
                    "camera_id": camera_id,
                    "event_id": str(event_id),
                    "event_received": ev is not None,
                    "evidence": bool(ev and ev.evidence),
                    "incident_id": str(ev.incident_id) if ev and ev.incident_id else None,
                }
            )
        incs = []
        for inc in incidents.values():
            own = [d for d in deliveries if d.incident_id == inc.id]
            first_event = min(
                (e.detected_at for e in events.values() if e.incident_id == inc.id), default=None
            )
            sent = [d.sent_at for d in own if d.sent_at is not None]
            latency = (min(sent) - first_event).total_seconds() if sent and first_event else None
            incs.append(
                {
                    "incident_id": str(inc.id),
                    "camera_ids": inc.camera_ids,
                    "area": inc.area,
                    "clip": inc.clip,
                    "keyframes": len(inc.keyframes),
                    "first_alert_seconds": round(latency, 3) if latency is not None else None,
                    "within_slo": latency is not None and latency <= slo,
                    "deliveries": [
                        {
                            "channel": d.channel,
                            "target": d.target,
                            "status": d.status,
                            "attempt": d.attempt,
                            "error": d.last_error,
                        }
                        for d in own
                    ],
                }
            )
        all_sent = bool(deliveries) and all(
            d.status == DeliveryStatus.SENT.value for d in deliveries
        )
        return {
            "id": str(run.id),
            "started_at": run.started_at.isoformat(),
            "started_by": run.started_by,
            "slo_seconds": slo,
            "cameras": cams,
            "incidents": incs,
            "all_delivered": all_sent,
            "clip_ready": bool(incs) and all(i["clip"] for i in incs),
            "confirmed_at": run.confirmed_at.isoformat() if run.confirmed_at else None,
            "confirmed_by": run.confirmed_by,
        }

    async def confirm(self, run_id: uuid.UUID, actor: str) -> dict[str, Any] | None:
        """Человек подтвердил, что алерты пришли: инциденты самотеста закрываются."""
        run = self.runs.get(run_id)
        if run is None:
            return None
        status = await self.status(run_id)
        for inc in (status or {}).get("incidents", []):
            try:
                await self.incidents.resolve(
                    uuid.UUID(inc["incident_id"]), actor, FeedbackVerdict.CONFIRMED, None, None
                )
            except Exception as exc:  # уже закрыт и т.п.
                log.info("selftest_resolve_skipped", incident=inc["incident_id"], reason=str(exc))
        run.confirmed_at, run.confirmed_by = datetime.now(UTC), actor
        async with self.sessions() as s:
            await write_audit(
                s,
                actor=actor,
                action="selftest_confirm",
                target=f"selftest:{run.id}",
                details={"all_delivered": bool(status and status["all_delivered"])},
            )
            await s.commit()
        return await self.status(run_id)

    # ------------------------------------------------------------ канарейка

    async def canary_once(self) -> dict[str, Any]:
        cfg = self.config().defaults.canary
        async with self.sessions() as s:
            cam = await s.scalar(
                select(models.Camera)
                .where(models.Camera.enabled.is_(True))
                .order_by(models.Camera.id)
            )
        if cam is None:
            return {"ok": None, "reason": "нет включённых камер"}
        started = datetime.now(UTC)
        event_id = uuid.uuid4()
        try:
            await send_command(
                self.valkey,
                PipelineCommand(
                    event_id=event_id,
                    kind="inject_event",
                    camera_id=cam.id,
                    event_type=EventType.FALL,
                    synthetic=SyntheticKind.CANARY,
                    issued_at=started,
                ),
            )
        except Exception as exc:
            return await self._canary_failed(cam.id, started, f"очередь команд недоступна: {exc}")
        stage = "событие не дошло от vision-pipeline"
        while (datetime.now(UTC) - started).total_seconds() < cfg.timeout_s:
            await asyncio.sleep(1)
            async with self.sessions() as s:
                ev = await s.get(models.Event, event_id)
                if ev is None or ev.incident_id is None:
                    continue
                stage = "инцидент создан, но алерт не доставлен в системный канал"
                sent = await s.scalar(
                    select(models.AlertDeliveryRow.sent_at).where(
                        models.AlertDeliveryRow.incident_id == ev.incident_id,
                        models.AlertDeliveryRow.status == DeliveryStatus.SENT.value,
                    )
                )
                incident_id = ev.incident_id
            if sent is not None:
                seconds = (sent - started).total_seconds()
                self.canary_seconds.observe(seconds)
                await self.system_alerts.clear_alert(SystemAlertKind.CANARY_FAILED, "canary")
                await self._close_canary(incident_id)
                self.last_canary = {
                    "ok": True,
                    "camera_id": cam.id,
                    "at": started.isoformat(),
                    "seconds": round(seconds, 3),
                }
                log.info("canary_ok", camera=cam.id, seconds=round(seconds, 3))
                return self.last_canary
        return await self._canary_failed(cam.id, started, stage)

    async def _close_canary(self, incident_id: uuid.UUID) -> None:
        try:
            await self.incidents.resolve(
                incident_id, "system:canary", FeedbackVerdict.CONFIRMED, None, None
            )
        except Exception as exc:
            log.info("canary_resolve_skipped", reason=str(exc))

    async def _canary_failed(self, camera_id: str, started: datetime, stage: str) -> dict[str, Any]:
        self.canary_failures.inc()
        timeout = self.config().defaults.canary.timeout_s
        await self.system_alerts.raise_alert(
            SystemAlertKind.CANARY_FAILED,
            SystemAlertSeverity.CRITICAL,
            "canary",
            f"Канарейка: тестовое событие на камере {camera_id} не прошло путь до алерта за "
            f"{timeout:g} с — {stage}. Алерты безопасности могут не доходить: проверьте "
            "vision-pipeline, core-api и очередь (qoima status).",
            details={"camera_id": camera_id, "stage": stage},
        )
        self.last_canary = {
            "ok": False,
            "camera_id": camera_id,
            "at": started.isoformat(),
            "stage": stage,
        }
        log.warning("canary_failed", camera=camera_id, stage=stage)
        return self.last_canary

    async def canary_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            cfg = self.config().defaults.canary
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=cfg.interval_minutes * 60)
            if stop.is_set():
                return
            if self.config().defaults.canary.alert_path_enabled:
                try:
                    await self.canary_once()
                except Exception as exc:
                    log.warning("canary_crashed", error=str(exc))
