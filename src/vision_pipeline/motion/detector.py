"""Дешёвый детектор движения: разность кадров на копии 160×90 (SPEC.md §5.4).

Нет движения — инференс раз в секунду («сторожевой» FPS), есть — целевой FPS сценария. Лежащий
неподвижно человек не даёт движения, поэтому сторожевой FPS обязателен: FallFSM должен видеть,
что человек всё ещё лежит, а таймер lying_seconds — продолжаться.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from shared.config.models import MotionConfig


class MotionDetector:
    def __init__(self, cfg: MotionConfig) -> None:
        self.cfg = cfg
        self._prev: NDArray[np.int16] | None = None

    def update(self, thumb: NDArray[np.uint8]) -> bool:
        """thumb — серая миниатюра cfg.width×cfg.height. True — в кадре есть движение."""
        cur = thumb.astype(np.int16)
        prev, self._prev = self._prev, cur
        if prev is None or prev.shape != cur.shape:
            return True  # первый кадр после подключения — анализируем
        changed = np.count_nonzero(np.abs(cur - prev) > self.cfg.pixel_delta)
        return bool(changed / cur.size > self.cfg.changed_ratio)


class FrameSampler:
    """Решает, отправлять ли кадр в инференс: не чаще целевого FPS при движении и не реже
    сторожевого без движения. Время — монотонное, в секундах.

    Расписание копится (следующий срок = прошлый срок + интервал), поэтому средний FPS равен
    целевому и при входном потоке, некратном ему (8 из 25 к/с, а не 25/4 = 6,25).
    """

    def __init__(self, idle_fps: float, active_hold_s: float) -> None:
        self.idle_fps = idle_fps
        self.active_hold_s = active_hold_s
        self._last: float | None = None
        self._next = 0.0
        self._active_until = 0.0

    def should_infer(self, t: float, motion: bool, target_fps: float) -> bool:
        if motion:
            self._active_until = t + self.active_hold_s
        fps = target_fps if t < self._active_until else self.idle_fps
        interval = 1.0 / fps
        base = t
        if self._last is not None:
            due = min(self._next, self._last + interval)  # FPS вырос — не ждём старого срока
            if t < due - 1e-6:
                return False
            if t - due < interval:
                base = due
        self._last = t
        self._next = base + interval
        return True
