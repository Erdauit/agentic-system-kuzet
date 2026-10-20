"""Инциденты и доставки на реальном Postgres: дедупликация между камерами одного участка,
микро-инциденты, at-least-once с повторами, недоступный Telegram не мешает веб-UI, исчерпание
повторов → системный алерт, эскалация, клип вдогонку, закрытие с кодом причины."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from prometheus_client import CollectorRegistry, Counter, Histogram
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core_api.alerts.channels import ChannelDispatcher
from core_api.alerts.delivery import DeliveryService
from core_api.alerts.system import SystemAlertManager
from core_api.db import models
from core_api.db.migrate import alembic_config
from core_api.incidents.manager import IncidentError, IncidentManager
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import (
    ClipRef,
    Event,
    EventMedia,
    EventType,
    FeedbackVerdict,
    FrameRef,
    IncidentState,
    ReasonCode,
    RoutingDecision,
    SystemAlertKind,
)
from shared.storage import LocalFSBlobStore

pytestmark = pytest.mark.integration

CHAT = "-100777"


@pytest.fixture
def migrated(database_url: str) -> str:
    command.upgrade(alembic_config(database_url), "head")
    return database_url


async def _clean(eng: AsyncEngine) -> None:
    async with eng.begin() as conn:
        for table in (
            models.AlertDeliveryRow,
            models.Feedback,
            models.Event,
            models.Incident,
            models.Zone,
            models.SystemAlert,
            models.Camera,
        ):
            await conn.execute(delete(table))


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    await _clean(eng)
    async with eng.begin() as conn:
        for cam in ("cam-01", "cam-02"):
            await conn.execute(
                models.Camera.__table__.insert().values(
                    id=cam,
                    name=f"Цех {cam[-1]}",
                    source="manual",
                    main_url=f"rtsp://10.0.0.{cam[-1]}/a",
                )
            )
        for zid, cam in (("press-a", "cam-01"), ("press-b", "cam-02")):
            await conn.execute(
                models.Zone.__table__.insert().values(
                    id=zid,
                    camera_id=cam,
                    name="Пресс",
                    kind="monitoring",
                    polygon={"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
                    area="press",
                )
            )
    yield eng
    await _clean(eng)
    await eng.dispose()


def tuned(config: QoimaConfig) -> QoimaConfig:
    data = config.model_dump(mode="json")
    alerts = data["alerts"]
    alerts["channels"]["telegram"].update(enabled=True, api_base="http://tg.test")
    alerts["groups"]["shift_operators"]["telegram_chat_ids"] = [CHAT]
    alerts["groups"]["shift_lead"]["telegram_chat_ids"] = ["-100888"]
    alerts["delivery"].update(max_attempts=3, initial_backoff_s=0.001, max_backoff_s=0.001)
    return QoimaConfig.model_validate(data)


class Rig:
    def __init__(self, engine: AsyncEngine, config: QoimaConfig, tmp: Path) -> None:
        self.telegram_up = True
        self.telegram_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not self.telegram_up:
                raise httpx.ConnectError("нет интернета")
            self.telegram_calls.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"ok": True})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.broadcaster = Broadcaster()
        self.queue = self.broadcaster.subscribe()
        reg = CollectorRegistry()
        self.latency = Histogram("alert_latency_seconds", "x", ["type"], registry=reg)
        dispatcher = ChannelDispatcher(self.http)
        self.alerts = SystemAlertManager(
            engine,
            None,
            self.broadcaster,
            dispatcher,
            lambda: config,
            dict,
            None,  # type: ignore[arg-type]
        )
        self.blobs = LocalFSBlobStore(tmp)
        self.blobs.put("frames/cam-01/e.jpg", b"\xff\xd8jpeg")
        self.blobs.put("clips/cam-01/c.mp4", b"mp4")
        self.delivery = DeliveryService(
            engine,
            dispatcher,
            self.broadcaster,
            lambda: config,
            self.blobs,
            self.alerts,
            lambda: {"cam-01": "Цех 1"},
            self.latency,
            Counter("alert_escalations", "x", ["level"], registry=reg),
        )
        dispatcher.web_hook = self.delivery.web_hook
        self.manager = IncidentManager(
            engine,
            None,  # type: ignore[arg-type]
            self.broadcaster,
            self.delivery,
            lambda: config,
            Counter("alerts", "x", ["severity"], registry=reg),
            Histogram("alert_ack_seconds", "x", ["severity"], registry=reg),
        )

    def messages(self, kind: str) -> list[dict[str, Any]]:
        out = []
        while not self.queue.empty():
            m = self.queue.get_nowait()
            if m.get("type") == kind:
                out.append(m)
        return out


@pytest.fixture
async def rig(
    engine: AsyncEngine, config: QoimaConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Rig]:
    token = tmp_path / "tg-token"
    token.write_text("123:abc")
    monkeypatch.setenv("QOIMA_TELEGRAM_TOKEN_FILE", str(token))
    r = Rig(engine, tuned(config), tmp_path)
    yield r
    await r.http.aclose()


def fall(
    camera: str, zone: str | None, area: str | None, at: datetime | None = None, **kw: Any
) -> Event:
    at = at or datetime.now(UTC)
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "type": EventType.FALL,
        "camera_id": camera,
        "zone_id": zone,
        "started_at": at - timedelta(seconds=10),
        "detected_at": at,
        "detector_score": 0.92,
        "routing": RoutingDecision.EVENT,
        "model_version": "test",
        "area": area,
        "evidence": [FrameRef(blob_key="frames/cam-01/e.jpg", captured_at=at)],
    }
    data.update(kw)
    return Event(**data)


async def statuses(engine: AsyncEngine, incident_id: uuid.UUID) -> dict[tuple[str, str], str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                models.AlertDeliveryRow.channel,
                models.AlertDeliveryRow.target,
                models.AlertDeliveryRow.status,
            ).where(models.AlertDeliveryRow.incident_id == incident_id)
        )
        return {(c, t): s for c, t, s in rows}


async def test_two_cameras_one_area_one_incident(rig: Rig, engine: AsyncEngine) -> None:
    first = fall("cam-01", "press-a", "press")
    inc1 = await rig.manager.handle_event(first)
    inc2 = await rig.manager.handle_event(fall("cam-02", "press-b", "press"))
    assert inc1 is not None and inc1 == inc2
    assert await rig.manager.handle_event(first) is None  # повтор из потока — без последствий
    recovered = fall("cam-01", None, None, type=EventType.FALL_RECOVERED)
    assert await rig.manager.handle_event(recovered) is None  # микро-инцидент без алерта
    view = await rig.manager.get(inc1)
    assert view is not None
    assert sorted(view.incident.camera_ids) == ["cam-01", "cam-02"] and len(view.events) == 2

    assert await rig.delivery.process_due() == 2  # web + telegram уровня 0
    assert await statuses(engine, inc1) == {("web_ui", "web"): "sent", ("telegram", CHAT): "sent"}
    assert rig.telegram_calls == ["sendPhoto"]  # алерт уходит сразу с кадром
    alerts = rig.messages("alert")
    assert alerts and alerts[0]["incident_id"] == str(inc1) and alerts[0]["sound"] is True
    samples = {s.name: s.value for m in rig.latency.collect() for s in m.samples}
    assert samples["alert_latency_seconds_count"] == 1

    # Клип готов → вдогонку в Telegram тем, кому ушёл алерт.
    clip = ClipRef(
        blob_key="clips/cam-01/c.mp4",
        starts_at=first.started_at,
        ends_at=first.detected_at,
        codec="h264",
    )
    await rig.manager.handle_media(EventMedia(event_id=first.id, camera_id="cam-01", clip=clip))
    assert await rig.delivery.process_due() == 1
    assert rig.telegram_calls == ["sendPhoto", "sendVideo"]
    view = await rig.manager.get(inc1)
    assert view is not None and view.incident.clip is not None

    with pytest.raises(IncidentError):
        await rig.manager.resolve(inc1, "operator:test", FeedbackVerdict.FALSE_ALARM, None, None)
    closed = await rig.manager.resolve(
        inc1, "operator:test", FeedbackVerdict.FALSE_ALARM, ReasonCode.REPAIR_LYING, "ремонт лёжа"
    )
    assert closed.state is IncidentState.FALSE_ALARM and closed.acknowledged_by == "operator:test"
    # После закрытия то же место — новый инцидент (fail-open).
    assert await rig.manager.handle_event(fall("cam-01", "press-a", "press")) not in (None, inc1)


async def test_telegram_down_web_still_delivered_then_system_alert(
    rig: Rig, engine: AsyncEngine
) -> None:
    rig.telegram_up = False
    inc = await rig.manager.handle_event(fall("cam-01", None, None))
    assert inc is not None
    for _ in range(3):
        await rig.delivery.process_due()
        async with engine.begin() as conn:  # пауза повтора уже прошла
            await conn.execute(
                update(models.AlertDeliveryRow).values(next_attempt_at=datetime.now(UTC))
            )
    st = await statuses(engine, inc)
    assert st[("web_ui", "web")] == "sent"
    assert st[("telegram", CHAT)] == "failed"
    active = await rig.alerts.active_alerts()
    assert [a.kind for a in active] == [SystemAlertKind.ALERT_DELIVERY_FAILED]
    assert active[0].component == "channel:telegram"
    # Telegram вернулся — следующая доставка снимает системный алерт.
    rig.telegram_up = True
    inc2 = await rig.manager.handle_event(fall("cam-02", None, None))
    assert inc2 is not None
    await rig.delivery.process_due()
    assert await rig.alerts.active_alerts() == []


async def test_escalation_until_ack(rig: Rig, engine: AsyncEngine, config: QoimaConfig) -> None:
    inc = await rig.manager.handle_event(fall("cam-01", None, None))
    assert inc is not None
    await rig.delivery.process_due()
    assert await rig.delivery.escalate() == 0  # рано
    lvl1 = config.alerts.safety.escalation[1].after_minutes
    async with engine.begin() as conn:
        await conn.execute(
            update(models.Incident)
            .where(models.Incident.id == inc)
            .values(opened_at=datetime.now(UTC) - timedelta(minutes=lvl1 + 0.1))
        )
    assert await rig.delivery.escalate() == 1
    await rig.delivery.process_due()
    st = await statuses(engine, inc)
    assert st[("telegram", "-100888")] == "sent"  # начальник смены — уровень 1
    view = await rig.manager.get(inc)
    assert view is not None and view.incident.escalation_level == 1
    await rig.manager.acknowledge(inc, "shift_lead:test")
    async with engine.begin() as conn:
        await conn.execute(
            update(models.Incident)
            .where(models.Incident.id == inc)
            .values(opened_at=datetime.now(UTC) - timedelta(hours=1))
        )
    assert await rig.delivery.escalate() == 0  # подтверждён — сирены не будет
    with pytest.raises(IncidentError):
        await rig.manager.acknowledge(inc, "operator:test")


async def test_new_selftest_does_not_merge_into_stale_one(rig: Rig) -> None:
    from shared.schemas import SyntheticKind

    old = await rig.manager.handle_event(
        fall("cam-01", "press-a", "press", synthetic=SyntheticKind.SELFTEST)
    )
    assert old is not None
    assert await rig.manager.close_synthetic(SyntheticKind.SELFTEST, "system:selftest") == 1
    view = await rig.manager.get(old)
    assert view is not None and view.incident.state is IncidentState.RESOLVED
    new = await rig.manager.handle_event(
        fall("cam-02", "press-b", "press", synthetic=SyntheticKind.SELFTEST)
    )
    assert new not in (None, old)
    # Служебный инцидент не сливается с настоящим на том же участке.
    real = await rig.manager.handle_event(fall("cam-01", "press-a", "press"))
    assert real not in (None, new)
