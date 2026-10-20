"""FallFSM (SPEC.md §5.8): STANDING → FALLING → DOWN → (lying_seconds) → ALERT.

Признаки по наблюдениям трека:
- «лежит»: корпус (середина плеч → середина бёдер) отклонён от вертикали на ≥ torso_lying_deg;
  без позы — бокс горизонтален (ширина ≥ bbox_lying_aspect × высоты);
- «падает»: центр масс опустился на ≥ drop_ratio роста за drop_window_s;
- «неподвижен»: центр масс сместился меньше чем на still_ratio роста.

Возврат в STANDING (стоит recover_seconds) сбрасывает FSM; если человек уже лежал (DOWN),
пишется микро-инцидент FALL_RECOVERED. Медленный переход в «лежит» без падения (ремонт лёжа)
тоже ведёт к ALERT после lying_seconds — это класс безопасности, решает человек и арбитр, —
но с пониженной уверенностью (событие чаще попадает в серую зону).
"""

from __future__ import annotations

import math
from enum import StrEnum

import numpy as np

from shared.config.models import FallScenario
from shared.schemas import EventType
from vision_pipeline.fsm.base import FsmSignal
from vision_pipeline.tracking.tracker import Observation

# Индексы COCO-17: плечи 5/6, бёдра 11/12.
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 5, 6, 11, 12
# Доля уверенности для падения без резкого смещения центра масс (медленно лёг).
SLOW_ONSET_FACTOR = 0.75


class FallState(StrEnum):
    STANDING = "standing"
    FALLING = "falling"
    DOWN = "down"
    ALERTED = "alerted"


def _mid(kp: np.ndarray, a: int, b: int, min_conf: float) -> tuple[float, float] | None:
    if kp[a, 2] < min_conf or kp[b, 2] < min_conf:
        return None
    return float((kp[a, 0] + kp[b, 0]) / 2), float((kp[a, 1] + kp[b, 1]) / 2)


def torso_angle(obs: Observation, aspect: float, min_conf: float) -> float | None:
    """Угол корпуса от вертикали кадра, градусы (0 — стоит, 90 — лежит). aspect = ширина/высота кадра."""
    if obs.keypoints is None:
        return None
    sh = _mid(obs.keypoints, L_SHOULDER, R_SHOULDER, min_conf)
    hip = _mid(obs.keypoints, L_HIP, R_HIP, min_conf)
    if sh is None or hip is None:
        return None
    dx = (sh[0] - hip[0]) * aspect
    dy = sh[1] - hip[1]
    if dx == 0 and dy == 0:
        return None
    return math.degrees(math.atan2(abs(dx), abs(dy)))


def center(obs: Observation, min_conf: float) -> tuple[float, float]:
    if obs.keypoints is not None:
        hip = _mid(obs.keypoints, L_HIP, R_HIP, min_conf)
        if hip is not None:
            return hip
    x1, y1, x2, y2 = obs.box
    return (x1 + x2) / 2, (y1 + y2) / 2


def height(obs: Observation) -> float:
    return max(obs.box[3] - obs.box[1], 1e-4)


class FallFSM:
    def __init__(self, cfg: FallScenario, aspect: float, keypoint_min: float) -> None:
        self.cfg = cfg
        self.aspect = aspect
        self.kp_min = keypoint_min
        self.state = FallState.STANDING
        self.standing_height: float | None = None
        self.fall_started: float | None = None
        self.down_since: float | None = None
        self.upright_since: float | None = None
        self.had_drop = False

    def is_lying(self, obs: Observation) -> bool:
        angle = torso_angle(obs, self.aspect, self.kp_min)
        if angle is not None:
            return angle >= self.cfg.torso_lying_deg
        x1, y1, x2, y2 = obs.box
        return (x2 - x1) * self.aspect >= self.cfg.bbox_lying_aspect * (y2 - y1)

    def _dropped(self, history: list[Observation], now: float) -> bool:
        ref_h = self.standing_height or height(history[-1])
        window = [o for o in history if now - o.t <= self.cfg.drop_window_s]
        if len(window) < 2:
            return False
        ys = [center(o, self.kp_min)[1] for o in window]
        return (ys[-1] - min(ys)) >= self.cfg.drop_ratio * ref_h

    def _still(self, history: list[Observation], now: float) -> bool:
        window = [o for o in history if now - o.t <= max(self.cfg.drop_window_s, 1.0)]
        if len(window) < 2:
            return True
        pts = np.array([center(o, self.kp_min) for o in window])
        spread = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
        ref = self.standing_height or height(window[-1])
        return spread <= self.cfg.still_ratio * ref

    def update(self, track_id: int, history: list[Observation]) -> FsmSignal | None:
        if not history:
            return None
        obs = history[-1]
        now = obs.t
        lying = self.is_lying(obs)

        if not lying:
            if self.state is FallState.STANDING:
                # Рост запоминаем только в вертикальном положении — от него меряем падение.
                self.standing_height = height(obs)
            self.upright_since = self.upright_since if self.upright_since is not None else now
        else:
            self.upright_since = None

        if self.state is FallState.STANDING:
            if lying:
                self.had_drop = self._dropped(history, now)
                self.fall_started = now
                self.state = FallState.FALLING
            elif self._dropped(history, now):
                self.fall_started = now
                self.had_drop = True
                self.state = FallState.FALLING
            return None

        if self.state is FallState.FALLING:
            if lying:
                self.state = FallState.DOWN
                self.down_since = now
            elif (
                self.upright_since is not None
                and now - self.upright_since >= self.cfg.recover_seconds
            ):
                self._reset()  # споткнулся и устоял — не микро-инцидент
            return None

        if self.state is FallState.DOWN:
            if not lying:
                if (
                    self.upright_since is not None
                    and now - self.upright_since >= self.cfg.recover_seconds
                ):
                    started = self.fall_started or now
                    self._reset()
                    return FsmSignal(EventType.FALL_RECOVERED, started, now, 1.0, [track_id])
                return None
            if self.down_since is not None and now - self.down_since >= self.cfg.lying_seconds:
                self.state = FallState.ALERTED
                base = sum(o.score for o in history[-10:]) / len(history[-10:])
                score = base if self.had_drop else base * SLOW_ONSET_FACTOR
                return FsmSignal(
                    EventType.FALL,
                    self.fall_started or self.down_since,
                    now,
                    round(min(max(score, 0.0), 1.0), 4),
                    [track_id],
                    details={"sharp_drop": self.had_drop, "still": self._still(history, now)},
                )
            return None

        # ALERTED: ждём, пока человек встанет, чтобы снова следить
        if (
            not lying
            and self.upright_since is not None
            and now - self.upright_since >= self.cfg.recover_seconds
        ):
            self._reset()
        return None

    def _reset(self) -> None:
        self.state = FallState.STANDING
        self.fall_started = None
        self.down_since = None
        self.had_drop = False
