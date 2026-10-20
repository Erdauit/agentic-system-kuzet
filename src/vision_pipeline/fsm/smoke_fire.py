"""SmokeFireFSM (SPEC.md §5.8): кандидат детектора + динамика (рост площади) за окно N секунд,
с учётом расписания плановых работ зоны (сварка, пропарка).

Работает только при наличии весов модели дыма/огня (configs/scenarios/smoke_fire.yaml: model).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from shared.config.models import SmokeFireScenario
from shared.schemas import EventType, WorkWindow, Zone
from vision_pipeline.fsm.base import FsmSignal


@dataclass(frozen=True)
class Candidate:
    t: float
    area: float  # суммарная площадь кандидатов в кадре (доля кадра)
    score: float


def in_work_window(windows: list[WorkWindow], moment: datetime) -> bool:
    wd, tm = moment.weekday(), moment.time()
    for w in windows:
        if w.start < w.end:
            if wd in w.weekdays and w.start <= tm < w.end:
                return True
        else:  # окно через полночь: часть до 24:00 — в указанный день, после 00:00 — на следующий
            if (wd in w.weekdays and tm >= w.start) or (
                ((wd - 1) % 7) in w.weekdays and tm < w.end
            ):
                return True
    return False


class SmokeFireFSM:
    def __init__(self, cfg: SmokeFireScenario, zone: Zone | None) -> None:
        self.cfg = cfg
        self.zone = zone
        self.window: deque[Candidate] = deque()
        self.alerted_until: float = 0.0

    def update(
        self, now: float, wall: datetime, candidates_area: float, score: float
    ) -> FsmSignal | None:
        if (
            self.cfg.respect_work_schedule
            and self.zone
            and in_work_window(self.zone.work_schedule, wall)
        ):
            self.window.clear()  # плановые работы: дым/искры ожидаемы
            return None
        if candidates_area > 0:
            self.window.append(Candidate(now, candidates_area, score))
        while self.window and now - self.window[0].t > self.cfg.window_seconds:
            self.window.popleft()
        if now < self.alerted_until or len(self.window) < self.cfg.min_candidate_frames:
            return None
        first, last = self.window[0], self.window[-1]
        growth = (last.area - first.area) / max(first.area, 1e-6)
        if growth < self.cfg.min_area_growth_ratio:
            return None
        self.alerted_until = now + self.cfg.window_seconds  # не повторять сработку в том же окне
        mean_score = sum(c.score for c in self.window) / len(self.window)
        return FsmSignal(
            EventType.SMOKE_FIRE,
            first.t,
            now,
            round(mean_score, 4),
            zone_id=self.zone.id if self.zone else None,
            details={"growth": round(growth, 3)},
        )
