"""Вход, блокировка, первый администратор и системные алерты — на реальном Postgres."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core_api.alerts.channels import ChannelDispatcher
from core_api.alerts.system import SystemAlertManager
from core_api.auth.service import AuthError, AuthService
from core_api.db import models
from core_api.db.migrate import alembic_config
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import CameraHealth, CameraHealthStatus, CameraIssue, Role

pytestmark = pytest.mark.integration


@pytest.fixture
def migrated(database_url: str) -> str:
    # Синхронно и вне event loop: env.py Alembic сам запускает asyncio.
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    async with eng.begin() as conn:
        for table in (models.SystemAlert, models.Session, models.User):
            await conn.execute(delete(table))
        await conn.execute(delete(models.Camera))
    yield eng
    await eng.dispose()


async def test_login_lockout_and_bootstrap(engine: AsyncEngine, config: QoimaConfig) -> None:
    auth = AuthService(engine, config.defaults.auth)
    assert await auth.bootstrap_admin("first-password-123") is True
    assert await auth.bootstrap_admin("again") is False  # только если пользователей нет
    token, user = await auth.login("admin", "first-password-123", "127.0.0.1", "pytest")
    assert user.role is Role.SITE_ADMIN and user.must_change_password
    assert (await auth.resolve(token)) == user
    await auth.change_password(user, "first-password-123", "second-password-456")
    with pytest.raises(AuthError):
        await auth.change_password(user, "wrong", "third-password-789")
    for _ in range(config.defaults.auth.max_failed_logins):
        with pytest.raises(AuthError):
            await auth.login("admin", "bad", None, None)
    with pytest.raises(AuthError, match="заблокирован"):
        await auth.login("admin", "second-password-456", None, None)
    await auth.logout(token, user)
    assert await auth.resolve(token) is None


async def test_system_alert_lifecycle(engine: AsyncEngine, config: QoimaConfig) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            models.Camera.__table__.insert().values(
                id="cam-01", name="Вход", source="manual", main_url="rtsp://10.0.0.1/a"
            )
        )
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        broadcaster = Broadcaster()
        queue = broadcaster.subscribe()
        manager = SystemAlertManager(
            engine,
            None,
            broadcaster,
            ChannelDispatcher(http),
            lambda: config,
            lambda: {"cam-01": "Вход"},
            None,  # type: ignore[arg-type]
        )
        now = datetime.now(UTC)
        down = CameraHealth(
            camera_id="cam-01",
            checked_at=now,
            status=CameraHealthStatus.DOWN,
            stream_alive=False,
            fps_in=0,
            fps_planned=15,
            fps_processed=0,
            reconnects_total=0,
            decode_errors_total=0,
            last_packet_at=now,
            frozen=False,
            black=False,
            overexposed=False,
            issues=[CameraIssue.STREAM_DOWN],
        )
        await manager.handle_health(down)
        await manager.handle_health(down)  # повтор не создаёт второй алерт
        active = await manager.active_alerts()
        assert len(active) == 1 and active[0].kind.value == "camera_down"
        assert "Вход" in active[0].message and "detection_seconds" in active[0].details
        acked = await manager.acknowledge(active[0].id, "site_admin:test")
        assert acked is not None and acked.acknowledged_by == "site_admin:test"
        ok = down.model_copy(
            update={"status": CameraHealthStatus.OK, "stream_alive": True, "issues": []}
        )
        await manager.handle_health(ok)
        assert await manager.active_alerts() == []
        events = [queue.get_nowait()["type"] for _ in range(queue.qsize())]
        assert "system_alert" in events and "camera_health" in events
        assert sent == []  # Telegram выключен — системные алерты не уходят наружу
        assert await manager.acknowledge(uuid.uuid4(), "x") is None
