"""Выборочный пик общей памяти GPU относительно исходного показания NVML.

Это не точный пик аллокатора: короткий всплеск между опросами может быть пропущен, а память
других процессов на той же карте тоже влияет на показания. Ошибка датчика — неизвестно, не 0.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from types import TracebackType
from typing import Self

MemoryReader = Callable[[], float | None]


def read_vram(read: MemoryReader | None) -> float | None:
    try:
        value = read() if read else None
    except Exception:  # отказ NVML не должен прерывать замер задержки
        return None
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return value


class VramPeak:
    def __init__(
        self, read: MemoryReader | None, baseline_mb: float | None, interval_s: float
    ) -> None:
        self._read = read
        self._baseline = baseline_mb
        self._interval = interval_s
        self._peak: float | None = None
        self._invalid = baseline_mb is None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        value = read_vram(self._read)
        if value is None or self._baseline is None or value < self._baseline:
            self._invalid = True
        else:
            self._peak = value if self._peak is None else max(self._peak, value)

    def _poll(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    def __enter__(self) -> Self:
        if self._read is not None and self._baseline is not None:
            self._sample()
            self._thread = threading.Thread(
                target=self._poll, name="qoima-vram-sampler", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._sample()

    @property
    def mb(self) -> int | None:
        if self._invalid or self._peak is None or self._baseline is None:
            return None
        return math.ceil(self._peak - self._baseline)
