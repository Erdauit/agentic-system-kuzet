"""Рассылка событий в UI по WebSocket (состояние камер, системные алерты, ход поиска камер).

core-api — единственный экземпляр, поэтому достаточно рассылки внутри процесса. Медленный
клиент не тормозит остальных: при переполнении его очереди старые сообщения отбрасываются.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

QUEUE_SIZE = 256


class Broadcaster:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(q)

    def publish(self, message: dict[str, Any]) -> int:
        """Разослать сообщение. Возвращает число подключённых окон UI, которым оно ушло."""
        queues = list(self._queues)
        for q in queues:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)
        return len(queues)

    @property
    def subscribers(self) -> int:
        return len(self._queues)
