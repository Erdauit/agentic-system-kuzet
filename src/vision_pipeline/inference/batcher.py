"""Батчинг кадров разных камер в одном вызове модели (SPEC.md §5.6).

Поток-исполнитель собирает запросы, пока не наберётся max_batch или не пройдёт
max_queue_delay_ms с первого запроса в очереди (≤ 20 мс), и отдаёт результаты через Future.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future


class Batcher[In, Out]:
    def __init__(
        self,
        fn: Callable[[list[In]], list[Out]],
        max_batch: int,
        max_delay_ms: float,
        on_batch: Callable[[int, float], None] | None = None,
    ) -> None:
        self.fn = fn
        self.max_batch = max_batch
        self.max_delay = max_delay_ms / 1000
        self.on_batch = on_batch
        self._q: queue.Queue[tuple[In, Future[Out]]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="inference-batcher", daemon=True)
        self._thread.start()

    def submit(self, item: In) -> Future[Out]:
        fut: Future[Out] = Future()
        self._q.put((item, fut))
        return fut

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.monotonic() + self.max_delay
            while len(batch) < self.max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._q.get(timeout=remaining))
                except queue.Empty:
                    break
            started = time.monotonic()
            try:
                results = self.fn([item for item, _ in batch])
                for (_, fut), res in zip(batch, results, strict=True):
                    fut.set_result(res)
            except Exception as exc:  # ошибка модели — каждому запросу, сервис продолжает
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(exc)
            if self.on_batch is not None:
                self.on_batch(len(batch), time.monotonic() - started)
