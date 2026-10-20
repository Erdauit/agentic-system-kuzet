"""Анализатор камеры и сервис hot path без камер и без моделей: детектор подменён сценарием,
видео — синтетический H.264 в GOP-буфере, Valkey — заглушка."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from prometheus_client import CollectorRegistry

from shared.config import QoimaConfig
from shared.schemas import (
    CameraRegistryEntry,
    EventMedia,
    EventType,
    PipelineCommand,
    Point,
    Polygon,
    RoutingDecision,
    ScenarioKind,
    SyntheticKind,
    Zone,
    ZoneKind,
)
from shared.storage.blobstore import LocalFSBlobStore
from tests.unit.media import fill, h264_stream
from vision_pipeline.hotpath.analyzer import CameraAnalyzer, Perception, available_scenarios
from vision_pipeline.hotpath.service import EVENTS_STREAM, HotPath, MediaJob
from vision_pipeline.tracking.tracker import Box, Detection

FRAME = np.zeros((360, 640, 3), np.uint8)
STAND: Box = (0.45, 0.3, 0.55, 0.9)
LIE: Box = (0.35, 0.75, 0.65, 0.9)


def zone(zid: str, kind: ZoneKind, pts: list[tuple[float, float]], area: str | None = None) -> Zone:
    return Zone(
        id=zid,
        camera_id="cam-01",
        name=zid,
        kind=kind,
        polygon=Polygon(points=[Point(x=x, y=y) for x, y in pts]),
        area=area,
    )


def entry(scenarios: list[ScenarioKind], zones: list[Zone] | None = None) -> CameraRegistryEntry:
    return CameraRegistryEntry(
        id="cam-01",
        name="Цех 1",
        enabled=True,
        main_path="cam-01/main",
        sub_path="cam-01/sub",
        scenarios=scenarios,
        zones=zones or [],
    )


class Script:
    """Детектор, который возвращает заранее заданный бокс в зависимости от времени."""

    def __init__(self, box_at: Callable[[float], Box | None]) -> None:
        self.box_at = box_at
        self.t = 0.0

    def __call__(self, rgb: np.ndarray) -> list[Detection]:
        box = self.box_at(self.t)
        return [Detection(box, 0.9)] if box else []


def lerp(a: Box, b: Box, k: float) -> Box:
    return tuple(x + (y - x) * k for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def fall_box(t: float) -> Box:
    if t < 3.0:
        return STAND
    if t < 3.6:  # падение за 0,6 с — промежуточные боксы, как на реальном видео при 5 к/с
        return lerp(STAND, LIE, (t - 3.0) / 0.6)
    return LIE


def run(an: CameraAnalyzer, script: Script, until: float, fps: float = 5.0) -> list[Any]:
    out = []
    for i in range(int(until * fps)):
        script.t = i / fps
        out += an.process(script.t, FRAME)
    return out


def test_no_detector_no_scenarios(config: QoimaConfig) -> None:
    p = Perception(None, None)
    assert available_scenarios(p) == set()
    an = CameraAnalyzer(entry([ScenarioKind.FALL]), config, p)
    assert not an.active and an.process(0.0, FRAME) == []


def test_fall_signal_after_lying_seconds(config: QoimaConfig) -> None:
    script = Script(fall_box)
    an = CameraAnalyzer(entry([ScenarioKind.FALL]), config, Perception(script, None, "det-1"))
    lying = config.scenarios[ScenarioKind.FALL].lying_seconds  # type: ignore[union-attr]
    early = run(an, script, 3.6 + lying - 0.5)
    assert [s.signal.type for s in early] == []
    signals = run(
        CameraAnalyzer(entry([ScenarioKind.FALL]), config, Perception(script, None)),
        script,
        3.6 + lying + 1,
    )
    assert [s.signal.type for s in signals] == [EventType.FALL]
    sig = signals[0].signal
    assert sig.details["sharp_drop"] is True  # резкое падение, не «медленно лёг»
    assert sig.detected_at - sig.started_at >= lying
    assert signals[0].box is not None and signals[0].area is None


def test_monitoring_zone_limits_fall_and_sets_area(config: QoimaConfig) -> None:
    lying = config.scenarios[ScenarioKind.FALL].lying_seconds  # type: ignore[union-attr]
    elsewhere = zone("mon-left", ZoneKind.MONITORING, [(0, 0), (0.3, 0), (0.3, 1), (0, 1)])
    script = Script(fall_box)
    an = CameraAnalyzer(entry([ScenarioKind.FALL], [elsewhere]), config, Perception(script, None))
    assert run(an, script, 3.6 + lying + 1) == []  # человек вне зоны контроля
    here = zone(
        "mon-center",
        ZoneKind.MONITORING,
        [(0.2, 0.2), (0.8, 0.2), (0.8, 1), (0.2, 1)],
        area="line-3",
    )
    an2 = CameraAnalyzer(entry([ScenarioKind.FALL], [here]), config, Perception(script, None))
    signals = run(an2, script, 3.6 + lying + 1)
    assert [(s.signal.zone_id, s.area) for s in signals] == [("mon-center", "line-3")]


def test_red_zone_dwell_and_area(config: QoimaConfig) -> None:
    red = zone(
        "press",
        ZoneKind.RED_ZONE,
        [(0.4, 0.8), (0.6, 0.8), (0.6, 1.0), (0.4, 1.0)],
        area="press-area",
    )
    script = Script(lambda t: STAND if t >= 1.0 else (0.1, 0.3, 0.2, 0.7))
    an = CameraAnalyzer(entry([ScenarioKind.RED_ZONE], [red]), config, Perception(script, None))
    signals = run(an, script, 4.0)
    assert [(s.signal.type, s.signal.zone_id, s.area) for s in signals] == [
        (EventType.RED_ZONE, "press", "press-area")
    ]
    # Без красных зон сценарий не активен — кадры не тратят инференс.
    assert not CameraAnalyzer(
        entry([ScenarioKind.RED_ZONE]), config, Perception(script, None)
    ).active


def test_wants_frame_follows_motion_and_idle_fps(config: QoimaConfig) -> None:
    script = Script(lambda t: None)
    an = CameraAnalyzer(entry([ScenarioKind.FALL]), config, Perception(script, None))
    still = np.zeros((90, 160), np.uint8)
    picks = [an.wants_frame(i / 25, still) for i in range(200)]  # 8 с без движения при 25 к/с
    hold = (
        config.defaults.pipeline.motion.active_hold_s
    )  # первый кадр после подключения — «движение»
    idle = [p for i, p in enumerate(picks) if i / 25 >= hold + 1]
    assert 4 <= sum(idle) <= 6  # дальше — сторожевой 1 FPS
    an2 = CameraAnalyzer(entry([ScenarioKind.FALL]), config, Perception(script, None))
    moving = [
        an2.wants_frame(i / 25, np.roll(np.eye(90, 160, dtype=np.uint8) * 255, i, axis=1))
        for i in range(100)
    ]
    fps = config.scenarios[ScenarioKind.FALL].target_fps
    assert sum(moving) >= 4 * fps - 1  # с движением — целевой FPS сценария


# ---------------------------------------------------------------- сервис hot path


class FakeValkey:
    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.added: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **_: Any) -> str:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("valkey недоступен")
        self.added.append((stream, fields))
        return "1-0"

    async def xread(self, *_: Any, **__: Any) -> list[Any]:
        await asyncio.sleep(0.05)
        return []


def hotpath(
    config: QoimaConfig, tmp_path: Path, valkey: FakeValkey, perception: Perception | None = None
) -> HotPath:
    return HotPath(
        valkey,  # type: ignore[arg-type]
        lambda: config,
        LocalFSBlobStore(tmp_path),
        "rtsp://mediamtx:8554",
        perception or Perception(None, None),
        CollectorRegistry(),
    )


async def test_selftest_event_and_clip_flow(config: QoimaConfig, tmp_path: Path) -> None:
    valkey = FakeValkey(fail_first=2)  # очередь недоступна — событие не теряется, а ждёт
    hp = hotpath(config, tmp_path, valkey)
    hp.bind(asyncio.get_running_loop())
    red = zone(
        "press",
        ZoneKind.RED_ZONE,
        [(0.4, 0.8), (0.6, 0.8), (0.6, 1.0), (0.4, 1.0)],
        area="press-area",
    )
    gop, sink = hp.attach(entry([], [red]))
    assert sink is None  # без детектора кадры не декодируются
    info, packets = h264_stream(frames=50)
    now = datetime.now(UTC).timestamp()
    fill(gop, info, packets, now - 5.0)
    hp.cameras["cam-01"].stop()  # mainstream-читатель в тесте не нужен
    hp.cameras["cam-01"].gop_main = None

    event_id = uuid4()
    cmd = PipelineCommand(
        event_id=event_id,
        kind="inject_event",
        camera_id="cam-01",
        event_type=EventType.FALL,
        synthetic=SyntheticKind.SELFTEST,
        zone_id="press",
        issued_at=datetime.now(UTC),
    )
    hp.inject(cmd)
    stop = asyncio.Event()
    task = asyncio.create_task(hp._publisher(stop))
    for _ in range(100):
        if valkey.added:
            break
        await asyncio.sleep(0.05)
    stream, fields = valkey.added[0]
    assert stream == EVENTS_STREAM
    from shared.schemas import Event

    event = Event.model_validate_json(fields["json"])
    assert event.id == event_id and event.synthetic is SyntheticKind.SELFTEST
    assert event.area == "press-area" and event.routing is RoutingDecision.EVENT
    assert event.evidence and (tmp_path / event.evidence[0].blob_key).is_file()
    stop.set()
    await task

    media = hp.extract(MediaJob(event_id, "cam-01", now - 3.0, now - 2.0, now))
    assert media.clip is not None and media.error is None
    assert (tmp_path / media.clip.blob_key).stat().st_size > 0
    assert media.clip.codec == "h264"
    assert 1 <= len(media.keyframes) <= config.defaults.incidents.keyframes_max
    assert all((tmp_path / k.blob_key).is_file() for k in media.keyframes)


async def test_media_without_video_reports_reason(config: QoimaConfig, tmp_path: Path) -> None:
    hp = hotpath(config, tmp_path, FakeValkey())
    hp.bind(asyncio.get_running_loop())
    hp.attach(entry([]))
    hp.cameras["cam-01"].stop()
    media = hp.extract(MediaJob(uuid4(), "cam-01", 0.0, 1.0, 2.0))
    assert isinstance(media, EventMedia) and media.clip is None and media.error
    gone = hp.extract(MediaJob(uuid4(), "cam-99", 0.0, 1.0, 2.0))
    assert gone.error and "отключена" in gone.error


def test_signal_below_t_low_is_dropped(config: QoimaConfig, tmp_path: Path) -> None:
    hp = hotpath(config, tmp_path, FakeValkey(), Perception(lambda rgb: [], None))
    loop = asyncio.new_event_loop()
    try:
        hp.bind(loop)
        hp.attach(entry([ScenarioKind.FALL]))
        cam = hp.cameras["cam-01"]
        cam.stop()
        from vision_pipeline.fsm.base import FsmSignal
        from vision_pipeline.hotpath.analyzer import SignalContext

        low = SignalContext(FsmSignal(EventType.FALL, 1.0, 11.0, 0.1, [1]), STAND, None)
        hp.emit(cam, low, FRAME)
        loop.run_until_complete(asyncio.sleep(0))
        assert hp.outbox is not None and hp.outbox.empty()
        ok = SignalContext(FsmSignal(EventType.FALL, 1.0, 11.0, 0.6, [1]), STAND, None)
        hp.emit(cam, ok, FRAME)
        loop.run_until_complete(asyncio.sleep(0))
        kind, (event, job) = hp.outbox.get_nowait()
        assert kind == "event" and event.routing is RoutingDecision.EVENT_WITH_ARBITER
        assert job.due == pytest.approx(11.0 + config.defaults.incidents.clip_post_s)
    finally:
        loop.close()
