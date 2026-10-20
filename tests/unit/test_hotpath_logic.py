"""Детерминированная логика hot path на синтетических треках (без видео и моделей)."""

from __future__ import annotations

from datetime import datetime, time

import numpy as np
import pytest

from shared.config import QoimaConfig
from shared.media.geometry import (
    CalibrationError,
    floor_homography,
    homography,
    point_in_polygon,
    project,
)
from shared.schemas import (
    EventType,
    Point,
    Polygon,
    RoutingDecision,
    ThresholdPair,
    WorkWindow,
    Zone,
    ZoneKind,
)
from vision_pipeline.fsm.fall import FallFSM, FallState
from vision_pipeline.fsm.red_zone import RedZoneFSM
from vision_pipeline.fsm.smoke_fire import SmokeFireFSM, in_work_window
from vision_pipeline.motion.detector import FrameSampler, MotionDetector
from vision_pipeline.router import route
from vision_pipeline.tracking.tracker import Detection, Observation, Tracker, iou

ASPECT = 16 / 9


def standing(t: float, x: float = 0.5, top: float = 0.3, score: float = 0.9) -> Observation:
    return Observation(t, (x - 0.04, top, x + 0.04, top + 0.4), score, None)


def lying(t: float, x: float = 0.5, score: float = 0.9) -> Observation:
    return Observation(t, (x - 0.2, 0.62, x + 0.2, 0.72), score, None)


# ---------------------------------------------------------------- геометрия


def test_homography_round_trip() -> None:
    src = [(0.2, 0.9), (0.8, 0.9), (0.7, 0.5), (0.3, 0.5)]
    h = homography(src, [(0, 0), (1, 0), (1, 1), (0, 1)])
    for (x, y), expected in zip(src, [(0, 0), (1, 0), (1, 1), (0, 1)], strict=True):
        assert project(h, (x, y)) == pytest.approx(expected, abs=1e-9)
    hm, metric = floor_homography(src, (0, 1, 4.0))
    assert metric
    a, b = project(hm, src[0]), project(hm, src[1])
    assert np.hypot(a[0] - b[0], a[1] - b[1]) == pytest.approx(4.0)
    with pytest.raises(CalibrationError):
        homography([(0, 0), (0.5, 0.5), (1, 1), (0.2, 0.2)], [(0, 0), (1, 0), (1, 1), (0, 1)])


def test_point_in_polygon() -> None:
    square = [(0.1, 0.1), (0.6, 0.1), (0.6, 0.7), (0.1, 0.7)]
    assert point_in_polygon((0.3, 0.3), square)
    assert not point_in_polygon((0.7, 0.3), square)


# ---------------------------------------------------------------- трекер


def test_tracker_keeps_identity_with_variable_dt(config: QoimaConfig) -> None:
    tr = Tracker(config.defaults.pipeline.tracker)
    t = 0.0
    ids = set()
    speed = 0.06  # человек идёт с постоянной скоростью: 6% ширины кадра в секунду
    for i in range(14):
        dt = 1.0 if i % 4 == 0 else 0.125  # адаптивный FPS: то 1, то 8 кадров в секунду
        t += dt
        x = 0.1 + speed * t
        confirmed = tr.update(t, [Detection((x, 0.3, x + 0.08, 0.7), 0.9)])
        ids |= {c.id for c in confirmed}
    assert ids == {1}
    # Скорость оценена по времени, а не по кадрам: предсказание на 1 с вперёд близко к истине.
    track = tr.tracks[0]
    track.kf.predict(1.0)
    assert track.box[0] == pytest.approx(0.1 + speed * (t + 1.0), abs=0.02)


def test_tracker_low_score_rescue_and_expiry(config: QoimaConfig) -> None:
    cfg = config.defaults.pipeline.tracker
    tr = Tracker(cfg)
    for i in range(4):
        tr.update(i * 0.2, [Detection((0.4, 0.3, 0.48, 0.7), 0.9)])
    # Частично загорожен: слабая детекция (0.2) продлевает тот же трек
    tr.update(1.0, [Detection((0.4, 0.3, 0.48, 0.7), 0.2)])
    assert [t.id for t in tr.tracks] == [1] and tr.tracks[0].hits == 5
    tr.update(1.0 + cfg.max_lost_s + 0.1, [])
    assert tr.tracks == []


def test_iou() -> None:
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1.0
    assert iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0


# ---------------------------------------------------------------- FallFSM


def run_fall(config: QoimaConfig, obs: list[Observation]) -> tuple[FallFSM, list[EventType]]:
    fsm = FallFSM(config.scenarios["fall"], ASPECT, 0.3)  # type: ignore[arg-type]
    events = []
    hist: list[Observation] = []
    for o in obs:
        hist.append(o)
        sig = fsm.update(7, hist)
        if sig is not None:
            events.append(sig.type)
    return fsm, events


def test_fall_alerts_after_lying_seconds(config: QoimaConfig) -> None:
    lying_s = config.scenarios["fall"].lying_seconds  # type: ignore[union-attr]
    obs = [standing(t / 8) for t in range(16)]
    obs += [
        Observation(2.0 + t / 8, (0.46, 0.3 + 0.1 * t, 0.54, 0.7 + 0.01 * t), 0.9, None)
        for t in range(3)
    ]
    obs += [lying(2.4 + t / 8) for t in range(int((lying_s + 1) * 8))]
    fsm, events = run_fall(config, obs)
    assert events == [EventType.FALL]
    assert fsm.state is FallState.ALERTED


def test_no_alert_if_lying_shorter_than_timer(config: QoimaConfig) -> None:
    obs = [standing(t / 8) for t in range(16)] + [lying(2 + t / 8) for t in range(40)]
    obs += [standing(7 + t / 8) for t in range(24)]
    _, events = run_fall(config, obs)
    assert events == [EventType.FALL_RECOVERED]  # микро-инцидент, не алерт


def test_slow_onset_has_lower_score(config: QoimaConfig) -> None:
    lying_s = config.scenarios["fall"].lying_seconds  # type: ignore[union-attr]
    fsm = FallFSM(config.scenarios["fall"], ASPECT, 0.3)  # type: ignore[arg-type]
    hist = [lying(t / 2) for t in range(int(lying_s * 2) + 6)]
    sigs = [s for i in range(1, len(hist) + 1) if (s := fsm.update(1, hist[:i])) is not None]
    assert sigs and sigs[0].type is EventType.FALL
    assert sigs[0].details["sharp_drop"] is False and sigs[0].score < 0.9


def test_pose_based_lying(config: QoimaConfig) -> None:
    fsm = FallFSM(config.scenarios["fall"], ASPECT, 0.3)  # type: ignore[arg-type]
    kp = np.zeros((17, 3))
    kp[[5, 6], :] = [[0.3, 0.6, 0.9], [0.3, 0.62, 0.9]]  # плечи
    kp[[11, 12], :] = [[0.5, 0.6, 0.9], [0.5, 0.62, 0.9]]  # бёдра на той же высоте — лежит
    assert fsm.is_lying(Observation(0, (0.25, 0.55, 0.6, 0.65), 0.9, kp))
    kp[[11, 12], 1] = 0.9  # бёдра ниже плеч — стоит
    kp[[11, 12], 0] = 0.3
    assert not fsm.is_lying(Observation(0, (0.25, 0.4, 0.35, 0.95), 0.9, kp))


# ---------------------------------------------------------------- RedZoneFSM


def zone(dwell: float | None = None) -> Zone:
    poly = Polygon(
        points=[Point(x=0.4, y=0.6), Point(x=0.9, y=0.6), Point(x=0.9, y=1.0), Point(x=0.4, y=1.0)]
    )
    return Zone(
        id="press-1",
        camera_id="cam-01",
        name="Пресс",
        kind=ZoneKind.RED_ZONE,
        polygon=poly,
        dwell_seconds=dwell,
    )


def test_red_zone_dwell_and_hysteresis(config: QoimaConfig) -> None:
    cfg = config.scenarios["red_zone"]
    fsm = RedZoneFSM(cfg, zone(dwell=1.0), 0.3)  # type: ignore[arg-type]
    out = [fsm.update(3, standing(t / 5, x=0.6)) for t in range(4)]  # 0.6 с внутри
    assert all(o is None for o in out)
    sig = None
    for t in range(4, 10):
        sig = sig or fsm.update(3, standing(t / 5, x=0.6))
    assert sig is not None and sig.type is EventType.RED_ZONE and sig.zone_id == "press-1"
    # Короткий выход (меньше гистерезиса) и возврат — повторного алерта нет
    assert fsm.update(3, standing(2.1, x=0.2)) is None
    assert all(fsm.update(3, standing(2.2 + t / 5, x=0.6)) is None for t in range(10))


def test_red_zone_outside_never_alerts(config: QoimaConfig) -> None:
    fsm = RedZoneFSM(config.scenarios["red_zone"], zone(), 0.3)  # type: ignore[arg-type]
    assert all(fsm.update(1, standing(t / 5, x=0.2)) is None for t in range(50))


# ---------------------------------------------------------------- SmokeFireFSM


def test_smoke_fire_growth_and_schedule(config: QoimaConfig) -> None:
    cfg = config.scenarios["smoke_fire"]
    z = Zone(
        id="boiler",
        camera_id="cam-02",
        name="Котельная",
        kind=ZoneKind.MONITORING,
        polygon=Polygon(points=[Point(x=0, y=0), Point(x=1, y=0), Point(x=1, y=1)]),
        work_schedule=[WorkWindow(activity="welding", weekdays=[0], start=time(9), end=time(12))],
    )
    fsm = SmokeFireFSM(cfg, z)  # type: ignore[arg-type]
    monday_work = datetime(2026, 10, 19, 10, 0)
    assert all(fsm.update(t / 3, monday_work, 0.01 * (1 + t), 0.8) is None for t in range(30))
    tuesday = datetime(2026, 10, 20, 10, 0)
    sig = None
    for t in range(30):
        sig = sig or fsm.update(t / 3, tuesday, 0.01 * (1 + t / 3), 0.8)
    assert sig is not None and sig.type is EventType.SMOKE_FIRE
    static = SmokeFireFSM(cfg, None)  # type: ignore[arg-type]
    assert all(
        static.update(t / 3, tuesday, 0.02, 0.8) is None for t in range(30)
    )  # без роста — нет


def test_work_window_over_midnight() -> None:
    w = [WorkWindow(activity="steaming", weekdays=[0], start=time(22), end=time(2))]
    assert in_work_window(w, datetime(2026, 10, 19, 23, 0))  # пн 23:00
    assert in_work_window(w, datetime(2026, 10, 20, 1, 0))  # вт 01:00 — продолжение окна пн
    assert not in_work_window(w, datetime(2026, 10, 20, 23, 0))


# ---------------------------------------------------------------- маршрутизация и движение


def test_router() -> None:
    th = ThresholdPair(t_low=0.4, t_high=0.8)
    assert route(0.9, th) is RoutingDecision.EVENT
    assert route(0.6, th) is RoutingDecision.EVENT_WITH_ARBITER
    assert route(0.39, th) is None


def test_motion_and_sampler(config: QoimaConfig) -> None:
    md = MotionDetector(config.defaults.pipeline.motion)
    still = np.full((90, 160), 100, dtype=np.uint8)
    assert md.update(still) is True  # первый кадр
    assert md.update(still) is False
    moved = still.copy()
    moved[20:60, 40:90] = 200
    assert md.update(moved) is True
    s = FrameSampler(idle_fps=1.0, active_hold_s=2.0)
    picks = [s.should_infer(t / 25, motion=False, target_fps=8) for t in range(50)]
    assert sum(picks) == 2  # 2 с без движения — 1 FPS
    picks = [s.should_infer(2 + t / 25, motion=True, target_fps=8) for t in range(25)]
    assert 7 <= sum(picks) <= 9  # с движением — целевые 8 FPS
