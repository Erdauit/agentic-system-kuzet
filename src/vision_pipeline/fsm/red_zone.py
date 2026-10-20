"""RedZoneFSM (SPEC.md §5.8): точка опоры человека внутри полигона дольше dwell_seconds,
с гистерезисом выхода. Точка опоры — середина лодыжек (поза) или середина нижней границы бокса."""

from __future__ import annotations

from enum import StrEnum

from shared.config.models import RedZoneScenario
from shared.media.geometry import point_in_polygon
from shared.schemas import EventType, Zone
from vision_pipeline.fsm.base import FsmSignal
from vision_pipeline.tracking.tracker import Observation

L_ANKLE, R_ANKLE = 15, 16


def foot_point(obs: Observation, min_conf: float) -> tuple[float, float]:
    kp = obs.keypoints
    if kp is not None and kp[L_ANKLE, 2] >= min_conf and kp[R_ANKLE, 2] >= min_conf:
        return float((kp[L_ANKLE, 0] + kp[R_ANKLE, 0]) / 2), float(
            (kp[L_ANKLE, 1] + kp[R_ANKLE, 1]) / 2
        )
    x1, _y1, x2, y2 = obs.box
    return (x1 + x2) / 2, y2


class ZoneState(StrEnum):
    OUTSIDE = "outside"
    INSIDE = "inside"
    ALERTED = "alerted"


class RedZoneFSM:
    """Один экземпляр на пару (трек, зона)."""

    def __init__(self, cfg: RedZoneScenario, zone: Zone, keypoint_min: float) -> None:
        self.cfg = cfg
        self.zone = zone
        self.kp_min = keypoint_min
        self.polygon = [(p.x, p.y) for p in zone.polygon.points]
        self.dwell = zone.dwell_seconds or cfg.dwell_seconds
        self.state = ZoneState.OUTSIDE
        self.entered_at: float | None = None
        self.outside_since: float | None = None

    def update(self, track_id: int, obs: Observation) -> FsmSignal | None:
        inside = point_in_polygon(foot_point(obs, self.kp_min), self.polygon)
        now = obs.t
        if inside:
            self.outside_since = None
            if self.state is ZoneState.OUTSIDE:
                self.state = ZoneState.INSIDE
                self.entered_at = now
            if (
                self.state is ZoneState.INSIDE
                and self.entered_at is not None
                and now - self.entered_at >= self.dwell
            ):
                self.state = ZoneState.ALERTED
                return FsmSignal(
                    EventType.RED_ZONE,
                    self.entered_at,
                    now,
                    round(obs.score, 4),
                    [track_id],
                    zone_id=self.zone.id,
                )
            return None
        if self.state is not ZoneState.OUTSIDE:
            self.outside_since = self.outside_since if self.outside_since is not None else now
            if now - self.outside_since >= self.cfg.exit_hysteresis_s:
                self.state = ZoneState.OUTSIDE
                self.entered_at = None
        return None
