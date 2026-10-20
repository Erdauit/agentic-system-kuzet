"""WebSocket /api/v1/ws: состояние камер, инциденты и алерты, системные алерты, ход поиска камер.

Авторизация — та же cookie-сессия. Системные алерты получают только site_admin и
qoima_engineer; остальным приходит лишь состояние камер.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core_api.auth.deps import ADMIN_ROLES, SESSION_COOKIE
from core_api.context import AppContext

router = APIRouter()

ADMIN_ONLY = {
    "system_alert",
    "discovery_progress",
    "discovery_done",
    "test_message",
    "canary_alert",
}


def _is_canary(message: dict[str, object]) -> bool:
    """Инциденты канарейки видят только инженеры: она не должна беспокоить операторов."""
    inc = message.get("incident")
    return isinstance(inc, dict) and inc.get("synthetic") == "canary"


@router.websocket("/api/v1/ws")
async def ws(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx
    token = websocket.cookies.get(SESSION_COOKIE)
    user = await ctx.auth.resolve(token) if token else None
    if user is None or user.must_change_password:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = ctx.broadcaster.subscribe()
    is_admin = user.role in ADMIN_ROLES

    async def drain_client() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await (
                    websocket.receive_text()
                )  # клиент ничего не шлёт; читаем, чтобы заметить закрытие

    reader = asyncio.create_task(drain_client())
    try:
        while not reader.done():
            try:
                message = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            if not is_admin and (message.get("type") in ADMIN_ONLY or _is_canary(message)):
                continue
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        ctx.broadcaster.unsubscribe(queue)
