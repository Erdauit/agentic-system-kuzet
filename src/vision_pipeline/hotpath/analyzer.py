"""Анализ одной камеры: движение → выбор кадра → детектор людей → поза → трекер → FSM.

Логика без видео и без моделей: кадры, детектор и поза приходят снаружи, поэтому модуль
целиком проверяется синтетическими данными (tests/unit/test_hotpath_analyzer.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from shared.config import QoimaConfig
from shared.config.models import FallScenario, RedZoneScenario
from shared.media.geometry import point_in_polygon
from shared.schemas import CameraRegistryEntry, ScenarioKind, Zone, ZoneKind
from vision_pipeline.fsm.base import FsmSignal
from vision_pipeline.fsm.fall import FallFSM
from vision_pipeline.fsm.red_zone import RedZoneFSM, foot_point
from vision_pipeline.motion.detector import FrameSampler, MotionDetector
from vision_pipeline.tracking.tracker import Box, Detection, Observation, Track, Tracker

RGB = NDArray[np.uint8]
DetectFn = Callable[[RGB], list[Detection]]
PoseFn = Callable[[RGB, list[Box]], list[NDArray[np.float64]]]


@dataclass(frozen=True)
class Perception:
    """Доступные модели. Нет детектора — нет сценариев с людьми (capabilities пусты)."""

    detect: DetectFn | None
    pose: PoseFn | None
    detector_version: str | None = None
    pose_version: str | None = None

    @property
    def model_version(self) -> str:
        parts = [v for v in (self.detector_version, self.pose_version) if v]
        return "+".join(parts) or "none"


def available_scenarios(perception: Perception) -> set[ScenarioKind]:
    """Сценарии, которые этот пайплайн реально может выполнять. Дым/огонь — только с весами."""
    if perception.detect is None:
        return set()
    return {ScenarioKind.FALL, ScenarioKind.RED_ZONE}


@dataclass
class SignalContext:
    """Сработка FSM и то, что нужно для события: бокс трека, участок зоны."""

    signal: FsmSignal
    box: Box | None
    area: str | None


@dataclass
class _ZoneSet:
    red: list[Zone] = field(default_factory=list)
    monitoring: list[Zone] = field(default_factory=list)


class CameraAnalyzer:
    def __init__(
        self, entry: CameraRegistryEntry, config: QoimaConfig, perception: Perception
    ) -> None:
        self.config = config
        self.perception = perception
        pipe = config.defaults.pipeline
        self.motion = MotionDetector(pipe.motion)
        self.sampler = FrameSampler(pipe.motion.idle_fps, pipe.motion.active_hold_s)
        self.tracker = Tracker(pipe.tracker)
        self.kp_min = pipe.inference.pose_keypoint_min
        self.fall_fsms: dict[int, FallFSM] = {}
        self.zone_fsms: dict[tuple[int, str], RedZoneFSM] = {}
        self.aspect: float | None = None
        self.configure(entry)

    def configure(self, entry: CameraRegistryEntry) -> None:
        """Применяет изменения реестра (сценарии, зоны) без переподключения к камере."""
        self.entry = entry
        enabled = set(entry.scenarios) & available_scenarios(self.perception)
        scen = self.config.scenarios
        fall = scen.get(ScenarioKind.FALL)
        red = scen.get(ScenarioKind.RED_ZONE)
        self.fall_cfg = (
            fall if isinstance(fall, FallScenario) and ScenarioKind.FALL in enabled else None
        )
        self.red_cfg = (
            red if isinstance(red, RedZoneScenario) and ScenarioKind.RED_ZONE in enabled else None
        )
        zones = _ZoneSet()
        for z in entry.zones:
            (zones.red if z.kind is ZoneKind.RED_ZONE else zones.monitoring).append(z)
        self.zones = zones
        live = {z.id for z in zones.red}
        self.zone_fsms = {k: v for k, v in self.zone_fsms.items() if k[1] in live}
        if self.fall_cfg is None:
            self.fall_fsms.clear()

    @property
    def active(self) -> bool:
        return self.fall_cfg is not None or (self.red_cfg is not None and bool(self.zones.red))

    @property
    def target_fps(self) -> float:
        fps = [c.target_fps for c in (self.fall_cfg, self.red_cfg) if c is not None]
        return max(fps) if fps else self.config.defaults.pipeline.motion.idle_fps

    def wants_frame(self, t: float, thumb: NDArray[np.uint8]) -> bool:
        """Вызывается на каждом кадре с серой миниатюрой; True — отправить кадр в инференс."""
        if not self.active:
            return False
        moving = self.motion.update(thumb)
        return self.sampler.should_infer(t, moving, self.target_fps)

    def process(self, t: float, rgb: RGB) -> list[SignalContext]:
        """Кадр, выбранный wants_frame: инференс, трекинг, FSM. Возвращает сработки."""
        detect = self.perception.detect
        if detect is None or not self.active:
            return []
        h, w = rgb.shape[:2]
        self.aspect = w / h
        dets = detect(rgb)
        pose = self.perception.pose
        if pose is not None and dets:
            strong = [
                d for d in dets if d.score >= self.config.defaults.pipeline.tracker.high_score
            ]
            kps = pose(rgb, [d.box for d in strong]) if strong else []
            with_kp = {id(d): k for d, k in zip(strong, kps, strict=True)}
            dets = [Detection(d.box, d.score, with_kp.get(id(d))) for d in dets]
        tracks = [tr for tr in self.tracker.update(t, dets) if tr.last_t == t]
        out: list[SignalContext] = []
        alive = self.tracker.active_ids()
        self.fall_fsms = {k: v for k, v in self.fall_fsms.items() if k in alive}
        self.zone_fsms = {k: v for k, v in self.zone_fsms.items() if k[0] in alive}
        for tr in tracks:
            obs = tr.history[-1]
            out.extend(self._fall(tr, obs))
            out.extend(self._red_zone(tr, obs))
        return out

    def _monitoring_zone(self, obs: Observation) -> tuple[bool, Zone | None]:
        """(анализировать ли, зона контроля). Без зон контроля анализируется весь кадр."""
        if not self.zones.monitoring:
            return True, None
        foot = foot_point(obs, self.kp_min)
        for z in self.zones.monitoring:
            if point_in_polygon(foot, [(p.x, p.y) for p in z.polygon.points]):
                return True, z
        return False, None

    def _fall(self, tr: Track, obs: Observation) -> list[SignalContext]:
        if self.fall_cfg is None or self.aspect is None:
            return []
        inside, zone = self._monitoring_zone(obs)
        if not inside:
            self.fall_fsms.pop(tr.id, None)  # вне зон контроля падения не отслеживаются
            return []
        fsm = self.fall_fsms.get(tr.id)
        if fsm is None:
            fsm = self.fall_fsms[tr.id] = FallFSM(self.fall_cfg, self.aspect, self.kp_min)
        signal = fsm.update(tr.id, list(tr.history))
        if signal is None:
            return []
        if zone is not None:
            signal = FsmSignal(
                signal.type,
                signal.started_at,
                signal.detected_at,
                signal.score,
                signal.track_ids,
                zone.id,
                signal.details,
            )
            return [SignalContext(signal, obs.box, zone.area)]
        return [SignalContext(signal, obs.box, None)]

    def _red_zone(self, tr: Track, obs: Observation) -> list[SignalContext]:
        if self.red_cfg is None:
            return []
        out = []
        for z in self.zones.red:
            key = (tr.id, z.id)
            fsm = self.zone_fsms.get(key)
            if fsm is None:
                fsm = self.zone_fsms[key] = RedZoneFSM(self.red_cfg, z, self.kp_min)
            signal = fsm.update(tr.id, obs)
            if signal is not None:
                out.append(SignalContext(signal, obs.box, z.area))
        return out
