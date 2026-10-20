"""Правила инцидентов и каналы доставки без БД: дедупликация, окно объединения, маршруты
самотеста и канарейки, тексты ru/kk, паузы повторов, Telegram (фото и клип), HTTP-реле и
сирена по Modbus/TCP на настоящем локальном сервере pymodbus."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from core_api.alerts.channels import AlertMessage, ChannelDispatcher, write_modbus_coil
from core_api.alerts.delivery import backoff_s
from core_api.incidents.policy import alert_text, dedup_key, joins, routes
from shared.config import QoimaConfig
from shared.config.models import RelayModbus
from shared.schemas import (
    ChannelKind,
    Event,
    EventType,
    IncidentState,
    Language,
    RoutingDecision,
    SyntheticKind,
)

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def event(camera: str = "cam-01", area: str | None = None, **kw: Any) -> Event:
    base: dict[str, Any] = {
        "id": uuid4(),
        "type": EventType.FALL,
        "camera_id": camera,
        "started_at": NOW,
        "detected_at": NOW,
        "detector_score": 0.9,
        "routing": RoutingDecision.EVENT,
        "model_version": "test",
        "area": area,
    }
    base.update(kw)
    return Event(**base)


def test_dedup_key_by_area_then_camera() -> None:
    assert dedup_key(event("cam-01", "press")) == dedup_key(event("cam-02", "press"))
    assert dedup_key(event("cam-01")) != dedup_key(event("cam-02"))
    assert dedup_key(event("cam-01", zone_id="z1")) != dedup_key(event("cam-01", zone_id="z2"))
    # Самотест не сливается с настоящим инцидентом того же участка.
    assert dedup_key(event(area="press", synthetic=SyntheticKind.SELFTEST)) != dedup_key(
        event(area="press")
    )
    assert dedup_key(event(type=EventType.RED_ZONE, area="press")) != dedup_key(event(area="press"))


def test_join_window_and_closed_incidents(config: QoimaConfig) -> None:
    cfg = config.defaults.incidents
    opened = NOW
    soon = NOW + timedelta(seconds=cfg.dedup_window_s - 1)
    assert joins(IncidentState.OPEN, opened, None, soon, cfg)
    assert joins(IncidentState.ACKNOWLEDGED, opened, None, soon, cfg)
    late = NOW + timedelta(seconds=max(cfg.dedup_window_s, cfg.cooldown_s) + 1)
    assert not joins(IncidentState.OPEN, opened, None, late, cfg)
    # Свежие события продлевают окно.
    assert joins(IncidentState.OPEN, opened, late - timedelta(seconds=5), late, cfg)
    # Закрытый инцидент не поглощает новое падение (fail-open).
    for closed in (IncidentState.RESOLVED, IncidentState.FALSE_ALARM):
        assert not joins(closed, opened, None, soon, cfg)


def test_routes_levels_selftest_canary(config: QoimaConfig) -> None:
    cfg = config.alerts
    lvl0 = routes(cfg, 0, None)
    assert {r.channel for r in lvl0} == set(cfg.safety.escalation[0].channels)
    selftest = routes(cfg, 0, SyntheticKind.SELFTEST)
    every = {ch for lvl in cfg.safety.escalation for ch in lvl.channels}
    assert {r.channel for r in selftest} == every  # самотест — во все каналы
    canary = routes(cfg, 0, SyntheticKind.CANARY)
    assert [(r.channel, r.web_target) for r in canary] == [(ChannelKind.WEB_UI, "system")]


def test_alert_text_ru_kk() -> None:
    ru = alert_text(
        Language.RU,
        EventType.FALL,
        "Цех 1",
        "Пресс",
        "08:00:10",
        RoutingDecision.EVENT_WITH_ARBITER,
        None,
        1,
    )
    assert ru.title == "Падение человека (требует проверки)"
    assert "«Цех 1»" in ru.text and "«Пресс»" in ru.text and "уровень 1" in ru.text
    kk = alert_text(
        Language.KK,
        EventType.SMOKE_FIRE,
        "Цех 1",
        None,
        "08:00:10",
        RoutingDecision.EVENT,
        SyntheticKind.SELFTEST,
        0,
    )
    assert kk.title.startswith("ӨЗІН-ӨЗІ ТЕКСЕРУ") and "Түтін" in kk.title
    clip = alert_text(
        Language.RU,
        EventType.FALL,
        "Цех 1",
        None,
        "08:00:10",
        RoutingDecision.EVENT,
        None,
        0,
        clip=True,
    )
    assert clip.title.startswith("Клип события") and "Подтвердите" not in clip.text


def test_backoff_grows_and_caps(config: QoimaConfig) -> None:
    d = config.alerts.delivery
    pauses = [backoff_s(a, d.initial_backoff_s, d.max_backoff_s) for a in range(1, 12)]
    assert pauses[:4] == [1, 2, 4, 8] and max(pauses) == d.max_backoff_s


def _with_telegram(config: QoimaConfig, chat: str = "-100123") -> QoimaConfig:
    alerts = config.alerts.model_copy(deep=True)
    tg = alerts.channels.telegram.model_copy(update={"enabled": True, "api_base": "http://tg.test"})
    channels = alerts.channels.model_copy(update={"telegram": tg})
    groups = dict(alerts.groups)
    groups["shift_operators"] = groups["shift_operators"].model_copy(
        update={"telegram_chat_ids": [chat]}
    )
    return config.model_copy(
        update={"alerts": alerts.model_copy(update={"channels": channels, "groups": groups})}
    )


async def test_telegram_photo_video_and_outage(
    config: QoimaConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token = tmp_path / "tg"
    token.write_text("123:abc")
    monkeypatch.setenv("QOIMA_TELEGRAM_TOKEN_FILE", str(token))
    cfg = _with_telegram(config).alerts
    calls: list[str] = []
    down = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if down["value"]:
            raise httpx.ConnectError("нет интернета")
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        d = ChannelDispatcher(http)
        assert d.targets(cfg, ChannelKind.TELEGRAM, ["shift_operators"]) == ["-100123"]
        photo = AlertMessage("Падение", "Камера 1", "critical", photo=b"\xff\xd8jpeg")
        assert (await d.send_one(cfg, ChannelKind.TELEGRAM, "-100123", photo)).ok
        video = AlertMessage("Клип", "Камера 1", "critical", video=b"mp4")
        assert (await d.send_one(cfg, ChannelKind.TELEGRAM, "-100123#clip", video)).ok
        plain = AlertMessage("Тест", "текст", "info")
        assert (await d.send_one(cfg, ChannelKind.TELEGRAM, "-100123", plain)).ok
        assert calls == [
            "/bot123:abc/sendPhoto",
            "/bot123:abc/sendVideo",
            "/bot123:abc/sendMessage",
        ]
        down["value"] = True
        res = await d.send_one(cfg, ChannelKind.TELEGRAM, "-100123", plain)
        assert not res.ok and res.error and "недоступен" in res.error
        # Веб-UI не зависит от Telegram.
        seen: list[str] = []

        def hook(target: str, msg: AlertMessage) -> int:
            seen.append(target)
            return windows

        d.web_hook = hook
        windows = 1
        assert (await d.send_one(cfg, ChannelKind.WEB_UI, "web", plain)).ok and seen == ["web"]
        # Никто не смотрит — не «доставлено»: повторы и системный алерт.
        windows = 0
        res = await d.send_one(cfg, ChannelKind.WEB_UI, "web", plain)
        assert not res.ok and res.error and "нет открытых окон" in res.error
        assert (await d.send_one(cfg, ChannelKind.WEB_UI, "system", plain)).ok  # канарейка


async def test_http_relay_and_disabled_channels(config: QoimaConfig) -> None:
    got: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        got.append(json.loads(request.content))
        return httpx.Response(204)

    relay = config.alerts.channels.local_relay.model_copy(
        update={
            "enabled": True,
            "http": config.alerts.channels.local_relay.http.model_copy(
                update={"url": "http://relay.test/siren"}
            ),
        }
    )
    cfg = config.alerts.model_copy(
        update={"channels": config.alerts.channels.model_copy(update={"local_relay": relay})}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        d = ChannelDispatcher(http)
        assert d.targets(cfg, ChannelKind.LOCAL_RELAY, []) == ["http"]
        msg = AlertMessage("Падение", "Камера 1", "critical", payload={"incident_id": "x"})
        assert (await d.send_one(cfg, ChannelKind.LOCAL_RELAY, "http", msg)).ok
        assert got[0]["incident_id"] == "x" and got[0]["source"] == "qoima-vision"
        # Выключенные каналы получателей не имеют, OPC UA честно не реализован.
        assert d.targets(config.alerts, ChannelKind.TELEGRAM, ["shift_operators"]) == []
        res = await d.send_one(cfg, ChannelKind.OPCUA, "opcua", msg)
        assert not res.ok


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def modbus_server() -> AsyncIterator[tuple[int, Any]]:
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )
    from pymodbus.server import ModbusTcpServer

    port = _free_port()
    device = ModbusDeviceContext(
        co=ModbusSequentialDataBlock(1, [False] * 16)
    )  # адресация блока с 1
    context = ModbusServerContext(devices={1: device}, single=False)
    server = ModbusTcpServer(context, address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    for _ in range(50):
        await asyncio.sleep(0.02)
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.1):
            break
    yield port, device
    await server.shutdown()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def test_modbus_siren_on_off(modbus_server: tuple[int, Any]) -> None:
    from pymodbus.client import AsyncModbusTcpClient

    port, _device = modbus_server
    cfg = RelayModbus(host="127.0.0.1", port=port, unit_id=1, coil=3, pulse_seconds=10)

    async def coil() -> bool:
        reader = AsyncModbusTcpClient("127.0.0.1", port=port)
        await reader.connect()
        try:
            res = await reader.read_coils(cfg.coil, count=1, device_id=1)
            return bool(res.bits[0])
        finally:
            reader.close()

    assert await write_modbus_coil(cfg, True, 2.0) is None
    assert await coil() is True
    assert await write_modbus_coil(cfg, False, 2.0) is None
    assert await coil() is False
    dead = RelayModbus(host="127.0.0.1", port=_free_port(), unit_id=1, coil=0, pulse_seconds=1)
    error = await write_modbus_coil(dead, True, 0.5)
    assert error is not None and "недоступно" in error
