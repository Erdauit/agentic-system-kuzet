"""Оценка состояния камеры по накопленной статистике потока. Чистая логика без ввода-вывода:
время передаётся явно, поэтому всё покрыто юнит-тестами без видео."""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from shared.config.models import CameraHealthConfig
from shared.media.quality import mean_abs_diff, occlusion_ratio
from shared.schemas import CameraHealth, CameraHealthStatus, CameraIssue, QualityBaseline
from vision_pipeline.health.analysis import FrameStats


@dataclass
class StreamStats:
    """Статистика потока одной камеры; пишет поток-читатель, читает оценщик (под замком)."""

    connected_once: bool = False
    last_packet_mono: float | None = None
    last_packet_wall: datetime | None = None
    packet_times: deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    # I-кадры приходят раз в 1–3 с: 16 кадров покрывают окно frozen_window_s с запасом.
    frames: deque[FrameStats] = field(default_factory=lambda: deque(maxlen=16))
    reconnects_total: int = 0
    decode_errors_total: int = 0
    frames_analyzed_times: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    inferred_times: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    view_shift_score: float | None = None


@dataclass(frozen=True)
class StatsSnapshot:
    connected_once: bool
    last_packet_mono: float | None
    last_packet_wall: datetime | None
    packet_times: list[float]
    frames: list[FrameStats]
    reconnects_total: int
    decode_errors_total: int
    analyzed_times: list[float]
    view_shift_score: float | None


@dataclass
class BaselineRef:
    baseline: QualityBaseline
    tiles: np.ndarray | None


class IssueDebouncer:
    """Проблема становится активной после raise_after подряд, снимается после clear_after чистых.
    STREAM_DOWN — без подтверждения: обнаружение офлайна ограничено SLO 15 с."""

    IMMEDIATE = frozenset({CameraIssue.STREAM_DOWN})

    def __init__(self, raise_after: int, clear_after: int) -> None:
        self.raise_after = raise_after
        self.clear_after = clear_after
        self.hits: dict[CameraIssue, int] = {}
        self.misses: dict[CameraIssue, int] = {}
        self.active: set[CameraIssue] = set()

    def update(self, observed: set[CameraIssue]) -> set[CameraIssue]:
        for issue in CameraIssue:
            if issue in observed:
                self.hits[issue] = self.hits.get(issue, 0) + 1
                self.misses[issue] = 0
                if issue in self.IMMEDIATE or self.hits[issue] >= self.raise_after:
                    self.active.add(issue)
            else:
                self.hits[issue] = 0
                self.misses[issue] = self.misses.get(issue, 0) + 1
                needed = 1 if issue in self.IMMEDIATE else self.clear_after
                if issue in self.active and self.misses[issue] >= needed:
                    self.active.discard(issue)
        return set(self.active)


def _rate(times: list[float], now: float, window: float) -> float:
    recent = [t for t in times if now - t <= window]
    if len(recent) < 2:
        return 0.0
    span = max(now - recent[0], 1e-6)
    return len(recent) / span


def observe(
    snap: StatsSnapshot,
    now_mono: float,
    cfg: CameraHealthConfig,
    baseline: BaselineRef | None,
    expected_fps: float | None,
    prev_reconnects: int,
    prev_decode_errors: int,
    started_mono: float,
) -> tuple[set[CameraIssue], dict[str, float | bool | None]]:
    """Наблюдаемые (ещё не подтверждённые) проблемы и сырые значения метрик."""
    issues: set[CameraIssue] = set()
    window = max(cfg.check_interval_s * 2, 5.0)
    fps_in = _rate(snap.packet_times, now_mono, window)
    alive = (
        snap.last_packet_mono is not None
        and now_mono - snap.last_packet_mono <= cfg.stream_timeout_s
    )
    values: dict[str, float | bool | None] = {
        "fps_in": round(fps_in, 2),
        "fps_processed": round(_rate(snap.analyzed_times, now_mono, max(window, 10.0)), 2),
        "alive": alive,
        "frozen": False,
        "black": False,
        "overexposed": False,
        "sharpness_ratio": None,
        "occluded_ratio": None,
        "brightness": None,
    }
    if not alive:
        # До первого пакета даём потоку stream_timeout_s на подключение — затем «нет потока».
        if snap.last_packet_mono is not None or now_mono - started_mono > cfg.stream_timeout_s:
            issues.add(CameraIssue.STREAM_DOWN)
        return issues, values

    if expected_fps and fps_in < cfg.low_fps_ratio * expected_fps:
        issues.add(CameraIssue.LOW_FPS)
    if snap.reconnects_total > prev_reconnects:
        issues.add(CameraIssue.RECONNECTING)
    if snap.decode_errors_total > prev_decode_errors:
        issues.add(CameraIssue.DECODE_ERRORS)

    recent = [f for f in snap.frames if now_mono - f.at <= cfg.frozen_window_s]
    span = recent[-1].at - recent[0].at if recent else 0.0
    if (
        len(recent) >= cfg.frozen_min_samples
        and span >= cfg.frozen_window_s / 2
        and all(mean_abs_diff(f.thumb, recent[0].thumb) <= cfg.frozen_max_mad for f in recent)
    ):
        issues.add(CameraIssue.FROZEN)
        values["frozen"] = True

    if snap.frames:
        last = snap.frames[-1]
        values["brightness"] = round(last.luma, 1)
        if last.luma <= cfg.black_luma_max:
            issues.add(CameraIssue.BLACK)
            values["black"] = True
        elif last.luma >= cfg.overexposed_luma_min:
            issues.add(CameraIssue.OVEREXPOSED)
            values["overexposed"] = True
        if baseline is not None and baseline.baseline.sharpness > 0 and not values["black"]:
            sharp = statistics.median(f.sharpness for f in snap.frames[-3:])
            ratio = sharp / baseline.baseline.sharpness
            values["sharpness_ratio"] = round(ratio, 3)
            if ratio < cfg.sharpness_drop_ratio:
                issues.add(CameraIssue.BLUR)
            if baseline.tiles is not None:
                occ = occlusion_ratio(last.tiles, baseline.tiles, cfg.occlusion_tile_texture_ratio)
                values["occluded_ratio"] = None if occ is None else round(occ, 3)
                if occ is not None and occ >= cfg.occlusion_ratio:
                    issues.add(CameraIssue.OCCLUDED)
    # Чёрный кадр и заслонение тоже «не узнаются» — сдвиг ракурса имеет смысл только без них.
    covered = issues & {CameraIssue.BLACK, CameraIssue.OCCLUDED, CameraIssue.OVEREXPOSED}
    if (
        not covered
        and snap.view_shift_score is not None
        and snap.view_shift_score >= cfg.view_shift_alert_score
    ):
        issues.add(CameraIssue.VIEW_SHIFT)
    return issues, values


def build_health(
    camera_id: str,
    snap: StatsSnapshot,
    active: set[CameraIssue],
    values: dict[str, float | bool | None],
    expected_fps: float | None,
    now_wall: datetime | None = None,
) -> CameraHealth:
    now = now_wall or datetime.now(UTC)
    issues = sorted(active, key=lambda i: list(CameraIssue).index(i))
    if CameraIssue.STREAM_DOWN in active:
        status = CameraHealthStatus.DOWN
    elif issues:
        status = CameraHealthStatus.DEGRADED
    elif not snap.connected_once:
        status = CameraHealthStatus.UNKNOWN
    else:
        status = CameraHealthStatus.OK
    down = CameraIssue.STREAM_DOWN in active

    def num(key: str) -> float | None:
        v = values.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    last_packet = snap.last_packet_wall
    if last_packet is not None and last_packet > now:
        last_packet = now - timedelta(milliseconds=1)
    return CameraHealth(
        camera_id=camera_id,
        checked_at=now,
        status=status,
        stream_alive=not down and bool(values.get("alive")),
        fps_in=num("fps_in") or 0.0 if not down else 0.0,
        fps_planned=expected_fps or 0.0,
        fps_processed=num("fps_processed") or 0.0 if not down else 0.0,
        reconnects_total=snap.reconnects_total,
        decode_errors_total=snap.decode_errors_total,
        last_packet_at=last_packet,
        brightness=num("brightness") if not down else None,
        frozen=CameraIssue.FROZEN in active,
        black=CameraIssue.BLACK in active,
        overexposed=CameraIssue.OVEREXPOSED in active,
        sharpness_ratio=num("sharpness_ratio") if not down else None,
        occluded_ratio=min(1.0, num("occluded_ratio") or 0.0)
        if not down and values.get("occluded_ratio") is not None
        else None,
        view_shift_score=snap.view_shift_score if not down else None,
        issues=issues,
    )
