"""Hot path всех камер (backend lite, SPEC.md §5): GOP-буферы, анализ кадров, события, клипы.

Потоки:
- поток-читатель substream каждой камеры (health/reader.py) декодирует кадры и вызывает
  CameraPipeline.on_frame: движение → выбор кадра → инференс (батч через камеры) → FSM;
- поток-читатель mainstream только складывает пакеты в GOP-буфер (без декодирования);
- asyncio: публикация событий (qoima:events) и медиа (qoima:event_media) в Valkey с повтором до
  успеха — событие безопасности не теряется из-за временной недоступности очереди;
- пул «clip»: через clip_post_s после события вырезает клип и ключевые кадры из GOP-буфера.

Команды core-api (qoima:pipeline:commands) — тестовые события самотеста и канарейки: они идут
тем же путём, что настоящие (кадр-доказательство, клип из буфера, публикация).
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from valkey.asyncio import Valkey

from shared.config import QoimaConfig
from shared.schemas import (
    BBox,
    CameraRegistryEntry,
    ClipRef,
    Event,
    EventMedia,
    EventType,
    FrameRef,
    PipelineCommand,
    RoutingDecision,
    ScenarioKind,
    ThresholdPair,
)
from shared.storage.blobstore import BlobStore
from shared.telemetry import create_metric, get_logger
from vision_pipeline.health.reader import FrameSink
from vision_pipeline.hotpath.analyzer import (
    CameraAnalyzer,
    Perception,
    SignalContext,
    available_scenarios,
)
from vision_pipeline.ingest.clip import decode_at, encode_jpeg, write_mp4
from vision_pipeline.ingest.gop import GopBuffer
from vision_pipeline.ingest.stream import StreamReader, hw_decoder
from vision_pipeline.router import route

log = get_logger("hot-path")

EVENTS_STREAM = "qoima:events"
MEDIA_STREAM = "qoima:event_media"
COMMANDS_STREAM = "qoima:pipeline:commands"
# Потоки событий ограничены по длине (Valkey MAXLEN ~): core-api переносит всё в Postgres.
STREAM_MAXLEN = 100_000
PUBLISH_RETRY_MAX_S = 5.0

_SCENARIO_OF = {
    EventType.FALL: ScenarioKind.FALL,
    EventType.FALL_RECOVERED: ScenarioKind.FALL,
    EventType.RED_ZONE: ScenarioKind.RED_ZONE,
    EventType.SMOKE_FIRE: ScenarioKind.SMOKE_FIRE,
}


def _dt(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


def _day(t: float) -> str:
    return _dt(t).strftime("%Y-%m-%d")


def _bbox(box: tuple[float, float, float, float] | None) -> BBox | None:
    if box is None:
        return None
    x1, y1, x2, y2 = (min(max(v, 0.0), 1.0) for v in box)
    return BBox(x1=x1, y1=y1, x2=x2, y2=y2) if x1 < x2 and y1 < y2 else None


@dataclass(frozen=True)
class MediaJob:
    event_id: UUID
    camera_id: str
    started_at: float
    detected_at: float
    due: float


class CameraPipeline:
    """GOP-буферы, читатель mainstream и анализатор одной камеры."""

    def __init__(self, entry: CameraRegistryEntry, hp: HotPath) -> None:
        self.hp = hp
        self.entry = entry
        pipe = hp.config().defaults.pipeline
        cap = int(pipe.gop_max_mb_per_stream * 1024 * 1024)
        self.gop_analysis = GopBuffer(pipe.gop_buffer_seconds, cap)
        self.gop_main: GopBuffer | None = None
        self.main_reader: StreamReader | None = None
        if entry.sub_path and entry.sub_path != entry.main_path:
            self.gop_main = GopBuffer(pipe.gop_buffer_seconds, cap)
            self.main_reader = StreamReader(
                f"main-{entry.id}", f"{hp.rtsp}/{entry.main_path}", hp.backoff, self.gop_main
            )
            self.main_reader.start()
        self.analyzer = CameraAnalyzer(entry, hp.config(), hp.perception)

    @property
    def clip_gop(self) -> GopBuffer:
        """Клип — из mainstream (качество для оператора), если он есть."""
        return self.gop_main or self.gop_analysis

    def stop(self) -> None:
        if self.main_reader is not None:
            self.main_reader.stop()

    def on_frame(self, frame: Any, t: float) -> bool:
        """Каждый декодированный кадр substream (поток-читатель камеры). True — ушёл в инференс."""
        hp = self.hp
        cfg = hp.config().defaults.pipeline
        thumb = np.asarray(
            frame.to_ndarray(format="gray", width=cfg.motion.width, height=cfg.motion.height),
            dtype=np.uint8,
        )
        if not self.analyzer.wants_frame(t, thumb):
            return False
        if frame.height > cfg.substream_max_height:
            h = cfg.substream_max_height
            w = int(round(frame.width * h / frame.height / 2) * 2)
            rgb = np.asarray(frame.to_ndarray(format="rgb24", width=w, height=h), np.uint8)
        else:
            rgb = np.asarray(frame.to_ndarray(format="rgb24"), np.uint8)
        started = time.monotonic()
        try:
            signals = self.analyzer.process(t, rgb)
        except TimeoutError:
            log.warning("inference_timeout", camera=self.entry.id)
            return False
        hp.m_stage.labels(stage="analyze").observe(time.monotonic() - started)
        for sc in signals:
            hp.emit(self, sc, rgb)
        return True


class HotPath:
    def __init__(
        self,
        valkey: Valkey,
        config: Callable[[], QoimaConfig],
        blobs: BlobStore,
        rtsp_base: str,
        perception: Perception,
        registry: CollectorRegistry,
    ) -> None:
        self.valkey = valkey
        self.config = config
        self.blobs = blobs
        self.rtsp = rtsp_base.rstrip("/")
        self.perception = perception
        self.cameras: dict[str, CameraPipeline] = {}
        self.hwaccel = hw_decoder(config().defaults.pipeline.decode)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clip")
        self.loop: asyncio.AbstractEventLoop | None = None
        self.outbox: asyncio.Queue[tuple[str, Any]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.m_stage = self._metric("pipeline_latency_seconds", registry, Histogram)
        self.m_events = self._metric("events_total", registry, Counter)
        self.m_gop = self._metric("gop_buffer_bytes", registry, Gauge)

    @staticmethod
    def _metric(name: str, registry: CollectorRegistry, kind: type) -> Any:
        metric = create_metric(name, registry)
        if not isinstance(metric, kind):
            raise TypeError(f"{name} в каталоге должен быть {kind.__name__}")
        return metric

    def backoff(self) -> tuple[float, float]:
        c = self.config().defaults.camera_health
        return c.reconnect_backoff_min_s, c.reconnect_backoff_max_s

    @property
    def capabilities(self) -> list[str]:
        caps = ["gop_buffer", "clips", "selftest_events"]
        if self.hwaccel is not None:
            caps.append("nvdec")
        scen = available_scenarios(self.perception)
        if self.perception.detect is not None:
            caps.append("person_detection")
        if self.perception.pose is not None:
            caps.append("pose")
        caps += sorted(s.value for s in scen)
        return caps

    # ------------------------------------------------------- связь с Camera Health

    def wants_frames(self, entry: CameraRegistryEntry) -> bool:
        return bool(set(entry.scenarios) & available_scenarios(self.perception))

    def attach(self, entry: CameraRegistryEntry) -> tuple[GopBuffer, FrameSink | None]:
        self.detach(entry.id)
        cam = CameraPipeline(entry, self)
        self.cameras[entry.id] = cam
        return cam.gop_analysis, (cam.on_frame if self.wants_frames(entry) else None)

    def update(self, entry: CameraRegistryEntry) -> None:
        cam = self.cameras.get(entry.id)
        if cam is not None:
            cam.entry = entry
            cam.analyzer.configure(entry)

    def detach(self, camera_id: str) -> None:
        cam = self.cameras.pop(camera_id, None)
        if cam is not None:
            cam.stop()

    # ------------------------------------------------------- события

    def thresholds(self, event_type: EventType) -> ThresholdPair | None:
        scen = self.config().scenarios.get(_SCENARIO_OF[event_type])
        return scen.thresholds if scen is not None else None

    def emit(self, cam: CameraPipeline, sc: SignalContext, rgb: np.ndarray) -> None:
        """Сработка FSM → Event (поток-читатель камеры)."""
        signal = sc.signal
        pair = self.thresholds(signal.type)
        decision = route(signal.score, pair) if pair is not None else RoutingDecision.EVENT
        if decision is None:
            log.info(
                "signal_below_t_low", camera=cam.entry.id, type=signal.type, score=signal.score
            )
            return
        event_id = uuid4()
        evidence = self._evidence(cam.entry.id, event_id, signal.detected_at, rgb, sc.box)
        event = Event(
            id=event_id,
            type=signal.type,
            camera_id=cam.entry.id,
            zone_id=signal.zone_id,
            track_ids=signal.track_ids,
            started_at=_dt(min(signal.started_at, signal.detected_at)),
            detected_at=_dt(signal.detected_at),
            detector_score=min(max(signal.score, 0.0), 1.0),
            routing=decision,
            model_version=self.perception.model_version,
            evidence=evidence,
            area=sc.area,
        )
        self.m_stage.labels(stage="frame_to_event").observe(
            max(0.0, time.time() - signal.detected_at)
        )
        self._enqueue(event, signal.started_at, signal.detected_at)

    def _evidence(
        self,
        camera_id: str,
        event_id: UUID,
        t: float,
        rgb: np.ndarray,
        box: tuple[float, float, float, float] | None,
    ) -> list[FrameRef]:
        key = f"frames/{camera_id}/{_day(t)}/{event_id}/evidence.jpg"
        try:
            self.blobs.put(key, encode_jpeg(rgb))
        except Exception as exc:  # без кадра алерт всё равно уходит
            log.warning("evidence_write_failed", camera=camera_id, error=str(exc))
            return []
        return [FrameRef(blob_key=key, captured_at=_dt(t), bbox=_bbox(box))]

    def _enqueue(self, event: Event, started: float, detected: float) -> None:
        post = self.config().defaults.incidents.clip_post_s
        job = MediaJob(event.id, event.camera_id, started, detected, detected + post)
        if self.loop is None or self.outbox is None:
            raise RuntimeError("HotPath.bind() не вызван")
        outbox = self.outbox
        self.loop.call_soon_threadsafe(outbox.put_nowait, ("event", (event, job)))

    # ------------------------------------------------------- самотест и канарейка

    def inject(self, cmd: PipelineCommand) -> None:
        """Тестовое событие: кадр «сейчас» из GOP-буфера камеры, дальше — обычный путь."""
        now = time.time()
        cam = self.cameras.get(cmd.camera_id)
        evidence: list[FrameRef] = []
        area = None
        if cam is not None:
            window = cam.gop_analysis.last_gop()
            frames = decode_at(window, [window.ends_at]) if window is not None else []
            if frames:
                evidence = self._evidence(
                    cmd.camera_id, cmd.event_id, frames[-1][0], frames[-1][1], None
                )
            zone = next((z for z in cam.entry.zones if z.id == cmd.zone_id), None)
            area = zone.area if zone else None
        event = Event(
            id=cmd.event_id,
            type=cmd.event_type,
            camera_id=cmd.camera_id,
            zone_id=cmd.zone_id,
            started_at=_dt(now),
            detected_at=_dt(now),
            detector_score=1.0,
            routing=RoutingDecision.EVENT,
            model_version="synthetic",
            evidence=evidence,
            area=area,
            synthetic=cmd.synthetic,
        )
        log.info(
            "synthetic_event", camera=cmd.camera_id, kind=cmd.synthetic, event_id=str(cmd.event_id)
        )
        self._enqueue(event, now, now)

    # ------------------------------------------------------- клип и ключевые кадры

    def extract(self, job: MediaJob) -> EventMedia:
        cam = self.cameras.get(job.camera_id)
        if cam is None:
            return EventMedia(
                event_id=job.event_id,
                camera_id=job.camera_id,
                error="камера отключена от пайплайна",
            )
        inc = self.config().defaults.incidents
        window = cam.clip_gop.window(
            job.started_at - inc.clip_pre_s, job.detected_at + inc.clip_post_s
        )
        if window is None or len(window.packets) < 2:
            return EventMedia(
                event_id=job.event_id,
                camera_id=job.camera_id,
                error="в GOP-буфере нет видео за время события (камера не передавала поток?)",
            )
        day = _day(job.detected_at)
        clip_key = f"clips/{job.camera_id}/{day}/{job.event_id}.mp4"
        with tempfile.TemporaryDirectory(prefix="qoima-clip-") as tmp:
            path = Path(tmp) / "clip.mp4"
            write_mp4(window, path)
            self.blobs.put_file(clip_key, path)
        clip = ClipRef(
            blob_key=clip_key,
            starts_at=_dt(window.starts_at),
            ends_at=_dt(max(window.ends_at, window.starts_at + 0.001)),
            codec=window.info.codec,
        )
        lo = max(window.starts_at, job.started_at - 1.0)
        hi = min(window.ends_at, job.detected_at + 1.0)
        n = inc.keyframes_max
        targets = [lo + (hi - lo) * i / max(n - 1, 1) for i in range(n)] if hi > lo else [lo]
        keyframes = []
        for i, (t, rgb) in enumerate(decode_at(window, targets)):
            key = f"frames/{job.camera_id}/{day}/{job.event_id}/key{i}.jpg"
            self.blobs.put(key, encode_jpeg(rgb))
            keyframes.append(FrameRef(blob_key=key, captured_at=_dt(t)))
        return EventMedia(
            event_id=job.event_id, camera_id=job.camera_id, clip=clip, keyframes=keyframes
        )

    async def _media_task(self, job: MediaJob) -> None:
        await asyncio.sleep(max(0.0, job.due - time.time()))
        loop = asyncio.get_running_loop()
        try:
            media = await loop.run_in_executor(self.pool, self.extract, job)
        except Exception as exc:
            log.exception("clip_failed", camera=job.camera_id, event_id=str(job.event_id))
            media = EventMedia(
                event_id=job.event_id, camera_id=job.camera_id, error=f"ошибка клипа: {exc}"[:500]
            )
        if self.outbox is not None:
            await self.outbox.put(("media", media))

    # ------------------------------------------------------- asyncio

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Привязка к циклу событий — до запуска читателей камер."""
        self.loop = loop
        self.outbox = asyncio.Queue()

    async def _publish(self, stream: str, data: str, stop: asyncio.Event) -> bool:
        delay = 0.5
        while not stop.is_set():
            try:
                await self.valkey.xadd(
                    stream, {"json": data}, maxlen=STREAM_MAXLEN, approximate=True
                )
                return True
            except Exception as exc:
                log.warning("publish_failed_retrying", stream=stream, error=str(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                delay = min(delay * 2, PUBLISH_RETRY_MAX_S)
        return False

    async def _publisher(self, stop: asyncio.Event) -> None:
        outbox = self.outbox
        if outbox is None:
            raise RuntimeError("HotPath.bind() не вызван")
        while not stop.is_set():
            try:
                kind, item = await asyncio.wait_for(outbox.get(), timeout=1.0)
            except TimeoutError:
                continue
            if kind == "event":
                event, job = item
                if await self._publish(EVENTS_STREAM, event.model_dump_json(), stop):
                    self.m_events.labels(type=event.type.value, camera=event.camera_id).inc()
                    log.info(
                        "event_published",
                        camera=event.camera_id,
                        type=event.type,
                        event_id=str(event.id),
                        routing=event.routing,
                    )
                task = asyncio.create_task(self._media_task(job))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                await self._publish(MEDIA_STREAM, item.model_dump_json(), stop)

    async def _commands(self, stop: asyncio.Event) -> None:
        last = "$"
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                resp = await self.valkey.xread({COMMANDS_STREAM: last}, block=1000, count=10)
            except Exception as exc:
                log.debug("commands_read_failed", error=str(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                continue
            for _stream, entries in resp or []:
                for msg_id, fields in entries:
                    last = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    raw = fields.get(b"json") or fields.get("json")
                    try:
                        cmd = PipelineCommand.model_validate_json(raw)
                    except Exception as exc:
                        log.warning("bad_pipeline_command", error=str(exc))
                        continue
                    await loop.run_in_executor(self.pool, self.inject, cmd)

    async def _gop_metrics(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for cam_id, cam in list(self.cameras.items()):
                total = cam.gop_analysis.size_bytes + (
                    cam.gop_main.size_bytes if cam.gop_main else 0
                )
                self.m_gop.labels(camera=cam_id).set(total)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5.0)

    async def run(self, stop: asyncio.Event) -> None:
        if self.loop is None:
            self.bind(asyncio.get_running_loop())
        tasks = [
            asyncio.create_task(self._publisher(stop)),
            asyncio.create_task(self._commands(stop)),
            asyncio.create_task(self._gop_metrics(stop)),
        ]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in [*tasks, *self._tasks]:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            for cam in self.cameras.values():
                cam.stop()
            self.pool.shutdown(wait=False, cancel_futures=True)
