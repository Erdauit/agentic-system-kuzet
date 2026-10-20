"""Трекер людей: фильтр Калмана с переменным dt + двухэтапное сопоставление в стиле ByteTrack.

FPS адаптивный (1 FPS без движения, до 10 FPS с движением), поэтому предсказание идёт по
реальному времени между кадрами, а не по номеру кадра (SPEC.md §5.5). Сопоставление жадное по
IoU: людей в кадре единицы-десятки, а результат совпадает с венгерским алгоритмом на
непересекающихся боксах и не требует scipy. Для сопоставления берётся лучшее IoU с предсказанием
и с последним наблюдением трека (устойчивость к резкой остановке движения).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from shared.config.models import TrackerConfig

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 в нормализованных координатах


@dataclass(frozen=True)
class Detection:
    box: Box
    score: float
    keypoints: NDArray[np.float64] | None = None  # (K, 3): x, y, уверенность


@dataclass(frozen=True)
class Observation:
    t: float
    box: Box
    score: float
    keypoints: NDArray[np.float64] | None


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class KalmanBox:
    """Состояние [cx, cy, w, h, vcx, vcy, vw, vh], модель постоянной скорости, шум ∝ dt."""

    # Шум процесса и измерения в нормализованных единицах кадра: человек за секунду смещается
    # на доли кадра, детектор ошибается на ~1–2% размера кадра.
    Q_POS, Q_VEL, R = 1e-3, 1e-2, 2.5e-4

    def __init__(self, box: Box) -> None:
        cx, cy, w, h = (
            (box[0] + box[2]) / 2,
            (box[1] + box[3]) / 2,
            box[2] - box[0],
            box[3] - box[1],
        )
        self.x = np.array([cx, cy, w, h, 0, 0, 0, 0], dtype=np.float64)
        self.p = np.diag([1e-3] * 4 + [1e-1] * 4)
        self.h = np.hstack([np.eye(4), np.zeros((4, 4))])

    def predict(self, dt: float) -> None:
        dt = max(dt, 0.0)
        f = np.eye(8)
        f[:4, 4:] = np.eye(4) * dt
        q = np.diag([self.Q_POS * dt] * 4 + [self.Q_VEL * dt] * 4)
        self.x = f @ self.x
        self.x[2:4] = np.maximum(self.x[2:4], 1e-4)
        self.p = f @ self.p @ f.T + q

    def update(self, box: Box) -> None:
        z = np.array(
            [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1]]
        )
        s = self.h @ self.p @ self.h.T + np.eye(4) * self.R
        k = self.p @ self.h.T @ np.linalg.inv(s)
        self.x = self.x + k @ (z - self.h @ self.x)
        self.p = (np.eye(8) - k @ self.h) @ self.p

    @property
    def box(self) -> Box:
        cx, cy, w, h = (float(v) for v in self.x[:4])
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


@dataclass
class Track:
    id: int
    kf: KalmanBox
    last_t: float
    hits: int = 1
    confirmed: bool = False
    history: deque[Observation] = field(default_factory=lambda: deque(maxlen=300))

    @property
    def box(self) -> Box:
        return self.kf.box

    @property
    def score(self) -> float:
        recent = list(self.history)[-10:]
        return sum(o.score for o in recent) / len(recent) if recent else 0.0


def _match(
    tracks: list[Track], dets: list[Detection], min_iou: float
) -> tuple[list[tuple[Track, Detection]], list[Track], list[Detection]]:
    # IoU берётся с предсказанным И с последним наблюдённым боксом: при резкой остановке
    # (падение закончилось на полу) скорость фильтра ещё несёт предсказание дальше, и без
    # второго варианта трек рвётся — вместе с историей падения.
    pairs = sorted(
        (
            (max(iou(t.box, d.box), iou(t.history[-1].box, d.box) if t.history else 0.0), ti, di)
            for ti, t in enumerate(tracks)
            for di, d in enumerate(dets)
        ),
        reverse=True,
    )
    used_t: set[int] = set()
    used_d: set[int] = set()
    matched: list[tuple[Track, Detection]] = []
    for score, ti, di in pairs:
        if score < min_iou:
            break
        if ti in used_t or di in used_d:
            continue
        used_t.add(ti)
        used_d.add(di)
        matched.append((tracks[ti], dets[di]))
    return (
        matched,
        [t for i, t in enumerate(tracks) if i not in used_t],
        [d for i, d in enumerate(dets) if i not in used_d],
    )


class Tracker:
    def __init__(self, cfg: TrackerConfig) -> None:
        self.cfg = cfg
        self.tracks: list[Track] = []
        self._next_id = 1

    def update(self, t: float, detections: list[Detection]) -> list[Track]:
        """Обновляет треки по детекциям кадра в момент t (секунды). Возвращает подтверждённые."""
        for tr in self.tracks:
            tr.kf.predict(t - tr.last_t)
        high = [d for d in detections if d.score >= self.cfg.high_score]
        low = [d for d in detections if self.cfg.low_score <= d.score < self.cfg.high_score]

        matched, rest_tracks, rest_high = _match(self.tracks, high, self.cfg.match_iou)
        matched2, _lost, _ = _match(rest_tracks, low, self.cfg.low_match_iou)
        for tr, det in [*matched, *matched2]:
            tr.kf.update(det.box)
            tr.last_t = t
            tr.hits += 1
            tr.confirmed = tr.confirmed or tr.hits >= self.cfg.min_hits
            tr.history.append(Observation(t, det.box, det.score, det.keypoints))
        for det in rest_high:
            tr = Track(
                id=self._next_id, kf=KalmanBox(det.box), last_t=t, confirmed=self.cfg.min_hits <= 1
            )
            tr.history.append(Observation(t, det.box, det.score, det.keypoints))
            self._next_id += 1
            self.tracks.append(tr)
        self.tracks = [tr for tr in self.tracks if t - tr.last_t <= self.cfg.max_lost_s]
        return [tr for tr in self.tracks if tr.confirmed]

    def active_ids(self) -> set[int]:
        return {tr.id for tr in self.tracks}
