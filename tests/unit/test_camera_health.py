"""Оценщик Camera Health: антидребезг, «нет потока» в пределах SLO, замерзание, расфокус,
чёрный кадр, сдвиг ракурса. Время задаётся явно — тесты без видео и без ожидания."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from shared.config import QoimaConfig
from shared.media.quality import laplacian_variance, mean_luma, thumbnail, tile_sharpness
from shared.schemas import CameraHealthStatus, CameraIssue, QualityBaseline
from vision_pipeline.health.analysis import FrameStats, analyze
from vision_pipeline.health.assessor import (
    BaselineRef,
    IssueDebouncer,
    StatsSnapshot,
    build_health,
    observe,
)
from vision_pipeline.health.view_shift import ViewShiftDetector

RNG = np.random.default_rng(7)
SCENE = RNG.integers(0, 256, size=(180, 320), dtype=np.uint8)


def frame(at: float, img: np.ndarray) -> FrameStats:
    return analyze(img, at, 320, (8, 6))[0]


def baseline() -> BaselineRef:
    q = QualityBaseline(
        sharpness=laplacian_variance(SCENE),
        brightness=mean_luma(SCENE),
        noise=1.0,
        snapshot_key="frames/cam-01/baseline.jpg",
        captured_at=datetime.now(UTC),
    )
    return BaselineRef(q, tile_sharpness(SCENE, (8, 6)))


def snap(
    frames: list[FrameStats], last_packet: float | None, packets: list[float], **kw: object
) -> StatsSnapshot:
    base: dict[str, object] = {
        "connected_once": last_packet is not None,
        "last_packet_mono": last_packet,
        "last_packet_wall": datetime.now(UTC) if last_packet else None,
        "packet_times": packets,
        "frames": frames,
        "reconnects_total": 0,
        "decode_errors_total": 0,
        "analyzed_times": [f.at for f in frames],
        "view_shift_score": None,
    }
    base.update(kw)
    return StatsSnapshot(**base)  # type: ignore[arg-type]


def live_frames(now: float) -> list[FrameStats]:
    return [frame(now - 8 + 2 * i, np.roll(SCENE, i * 3, axis=1)) for i in range(5)]


def test_healthy_camera(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    now = 100.0
    packets = [now - 10 + i / 15 for i in range(150)]
    issues, values = observe(
        snap(live_frames(now), now - 0.05, packets), now, cfg, baseline(), 15.0, 0, 0, 0.0
    )
    assert issues == set()
    assert 13 < float(values["fps_in"] or 0) < 17


def test_stream_down_after_timeout_within_slo(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    last = 100.0
    early, _ = observe(
        snap([], last, [last]), last + cfg.stream_timeout_s - 0.5, cfg, None, 15.0, 0, 0, 0.0
    )
    assert CameraIssue.STREAM_DOWN not in early
    late, _ = observe(
        snap([], last, [last]), last + cfg.stream_timeout_s + 0.5, cfg, None, 15.0, 0, 0, 0.0
    )
    assert CameraIssue.STREAM_DOWN in late
    # Худший случай: таймаут + один интервал проверки — в пределах SLO 15 с.
    assert (
        cfg.stream_timeout_s + cfg.check_interval_s
        <= config.defaults.slo.camera_offline_detection_s
    )


def test_never_connected_camera_goes_down(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    assert observe(snap([], None, []), 5.0, cfg, None, None, 0, 0, 0.0)[0] == set()
    assert (
        CameraIssue.STREAM_DOWN
        in observe(snap([], None, []), cfg.stream_timeout_s + 1, cfg, None, None, 0, 0, 0.0)[0]
    )


def test_frozen_detected_only_for_identical_frames(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    now = 100.0
    frozen = [frame(now - 8 + 2 * i, SCENE) for i in range(5)]
    packets = [now - 10 + i / 15 for i in range(150)]
    assert (
        CameraIssue.FROZEN
        in observe(snap(frozen, now, packets), now, cfg, baseline(), 15.0, 0, 0, 0.0)[0]
    )
    assert (
        CameraIssue.FROZEN
        not in observe(snap(live_frames(now), now, packets), now, cfg, baseline(), 15.0, 0, 0, 0.0)[
            0
        ]
    )


def test_blur_black_and_occlusion(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    now = 100.0
    packets = [now - 10 + i / 15 for i in range(150)]
    k = 9
    blurred = np.clip(
        sum(
            np.roll(np.roll(SCENE.astype(float), dy, 0), dx, 1)
            for dy in range(k)
            for dx in range(k)
        )
        / (k * k),
        0,
        255,
    ).astype(np.uint8)
    blur_frames = [frame(now - 8 + 2 * i, np.roll(blurred, i, axis=1)) for i in range(5)]
    issues, values = observe(snap(blur_frames, now, packets), now, cfg, baseline(), 15.0, 0, 0, 0.0)
    assert (
        CameraIssue.BLUR in issues
        and float(values["sharpness_ratio"] or 1) < cfg.sharpness_drop_ratio
    )
    black = [
        frame(now - 8 + 2 * i, np.full((180, 320), 10 + i % 2, dtype=np.uint8)) for i in range(5)
    ]
    issues, _ = observe(
        snap(black, now, packets, view_shift_score=1.0), now, cfg, baseline(), 15.0, 0, 0, 0.0
    )
    assert CameraIssue.BLACK in issues
    assert CameraIssue.VIEW_SHIFT not in issues  # чёрный кадр не выдаём за сдвиг ракурса
    assert CameraIssue.BLUR not in issues


def test_low_fps_and_reconnects(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    now = 100.0
    packets = [now - 10 + i / 3 for i in range(30)]  # 3 FPS при ожидаемых 15
    issues, _ = observe(
        snap(live_frames(now), now, packets, reconnects_total=2),
        now,
        cfg,
        baseline(),
        15.0,
        1,
        0,
        0.0,
    )
    assert {CameraIssue.LOW_FPS, CameraIssue.RECONNECTING} <= issues


def test_debouncer() -> None:
    d = IssueDebouncer(raise_after=2, clear_after=3)
    assert d.update({CameraIssue.BLUR}) == set()
    assert d.update({CameraIssue.BLUR}) == {CameraIssue.BLUR}
    assert d.update(set()) == {CameraIssue.BLUR}
    assert d.update(set()) == {CameraIssue.BLUR}
    assert d.update(set()) == set()
    # «Нет потока» — сразу и снимается сразу.
    assert d.update({CameraIssue.STREAM_DOWN}) == {CameraIssue.STREAM_DOWN}
    assert d.update(set()) == set()


def test_build_health_contract_consistency() -> None:
    s = snap([], 1.0, [1.0])
    down = build_health("cam-01", s, {CameraIssue.STREAM_DOWN}, {"alive": False}, 15.0)
    assert down.status is CameraHealthStatus.DOWN and not down.stream_alive
    ok = build_health(
        "cam-01",
        snap([frame(1.0, SCENE)], 1.0, [1.0]),
        set(),
        {"alive": True, "fps_in": 15.0},
        15.0,
    )
    assert ok.status is CameraHealthStatus.OK
    frozen = build_health("cam-01", s, {CameraIssue.FROZEN}, {"alive": True}, 15.0)
    assert frozen.frozen and frozen.status is CameraHealthStatus.DEGRADED


def test_view_shift_scores(config: QoimaConfig) -> None:
    cfg = config.defaults.camera_health
    det = ViewShiftDetector(
        cfg.view_shift_orb_features, cfg.view_shift_full_displacement, cfg.view_shift_min_inliers
    )
    # Структурная сцена (прямоугольники разной яркости) — ORB находит устойчивые углы.
    scene = np.full((180, 320), 40, dtype=np.uint8)
    for _ in range(12):
        x, y = int(RNG.integers(0, 280)), int(RNG.integers(0, 150))
        scene[y : y + 25, x : x + 35] = int(RNG.integers(80, 250))
    ref = det.reference(scene)
    assert ref is not None
    assert det.score(ref, scene) < 0.2
    assert (
        det.score(ref, np.roll(scene, 2, axis=1)) < cfg.view_shift_alert_score
    )  # мелкая вибрация — не сдвиг
    crop = scene[60:, 120:]
    zoomed = np.kron(crop, np.ones((2, 2), dtype=np.uint8))[:180, :320]
    assert det.score(ref, zoomed) >= cfg.view_shift_alert_score
    assert (
        det.reference(np.full((180, 320), 30, dtype=np.uint8)) is None
    )  # нет текстуры — нет эталона
    assert thumbnail(scene).shape == (90, 160)
