"""Camera Health для всех камер реестра: читатели потоков, оценка раз в check_interval_s,
публикация в Valkey Streams (для core-api) и в Prometheus.

Реестр камер публикует core-api (ключ qoima:cameras, канал qoima:cameras:changed); учётных
данных в нём нет — потоки читаются из MediaMTX.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from prometheus_client import CollectorRegistry, Counter, Gauge
from valkey.asyncio import Valkey

from shared.config import QoimaConfig
from shared.config.models import CameraHealthConfig
from shared.media.quality import tile_sharpness, to_analysis_size
from shared.schemas import CameraHealth, CameraIssue, CameraRegistryEntry
from shared.telemetry import create_metric, get_logger
from vision_pipeline.health.assessor import BaselineRef, IssueDebouncer, build_health, observe
from vision_pipeline.health.reader import CameraReader
from vision_pipeline.health.view_shift import Reference, ViewShiftDetector

log = get_logger("camera-health")

REGISTRY_KEY = "qoima:cameras"
REGISTRY_CHANNEL = "qoima:cameras:changed"
CONFIG_CHANNEL = "qoima:config:changed"
HEALTH_STREAM = "qoima:camera_health"
LATEST_KEY = "qoima:camera_health:latest:{camera}"
# Поток снимков ограничен ~сутками для 100 камер при опросе раз в 5 с (Valkey MAXLEN ~).
STREAM_MAXLEN = 200_000
REGISTRY_POLL_S = 30


@dataclass
class CameraWorker:
    entry: CameraRegistryEntry
    reader: CameraReader
    debouncer: IssueDebouncer
    started_mono: float
    baseline: BaselineRef | None
    # Ссылка на эталон ORB, общая с потоком-читателем (меняется при новой базовой линии).
    reference: dict[str, Reference | None]
    prev_reconnects: int = 0
    prev_decode_errors: int = 0


def _ref_getter(holder: dict[str, Reference | None]) -> Callable[[], Reference | None]:
    def get() -> Reference | None:
        return holder["ref"]

    return get


class CameraHealthService:
    def __init__(
        self,
        valkey: Valkey,
        config: Callable[[], QoimaConfig],
        reload_config: Callable[[], None],
        mediamtx_rtsp_url: str,
        data_root: Path,
        registry: CollectorRegistry,
    ) -> None:
        self.valkey = valkey
        self.config = config
        self.reload_config = reload_config
        self.rtsp = mediamtx_rtsp_url.rstrip("/")
        self.data_root = data_root
        self.workers: dict[str, CameraWorker] = {}
        cfg = config().defaults.camera_health
        self.detector = ViewShiftDetector(
            cfg.view_shift_orb_features,
            cfg.view_shift_full_displacement,
            cfg.view_shift_min_inliers,
        )
        self.m_fps_in = self._gauge("camera_fps_in", registry)
        self.m_fps_processed = self._gauge("camera_fps_processed", registry)
        self.m_frozen = self._gauge("camera_frozen", registry)
        self.m_sharpness = self._gauge("camera_sharpness", registry)
        self.m_view_shift = self._gauge("camera_view_shift_score", registry)
        reconnects = create_metric("camera_reconnects_total", registry)
        decode_errors = create_metric("decode_errors_total", registry)
        if not isinstance(reconnects, Counter) or not isinstance(decode_errors, Counter):
            raise TypeError("счётчики Camera Health в каталоге должны быть counter")
        self.m_reconnects = reconnects
        self.m_decode_errors = decode_errors
        self.last_publish_mono: float | None = None

    @staticmethod
    def _gauge(name: str, registry: CollectorRegistry) -> Gauge:
        metric = create_metric(name, registry)
        if not isinstance(metric, Gauge):
            raise TypeError(f"{name} в каталоге должен быть gauge")
        return metric

    # ------------------------------------------------------------ реестр

    async def load_registry(self) -> list[CameraRegistryEntry]:
        raw = await self.valkey.get(REGISTRY_KEY)
        if not raw:
            return []
        return [CameraRegistryEntry.model_validate(e) for e in json.loads(raw)]

    def _load_baseline(
        self, entry: CameraRegistryEntry
    ) -> tuple[BaselineRef | None, Reference | None]:
        if entry.baseline is None:
            return None, None
        cfg = self.config().defaults.camera_health
        path = self.data_root / entry.baseline.snapshot_key
        tiles = None
        reference = None
        if path.is_file():
            img = cv2.imdecode(
                np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE
            )
            if img is not None:
                small = to_analysis_size(np.asarray(img, dtype=np.uint8), cfg.analysis_width)
                tiles = tile_sharpness(small, cfg.occlusion_grid)
                reference = self.detector.reference(small)
        else:
            log.warning("baseline_snapshot_missing", camera=entry.id, path=str(path))
        return BaselineRef(entry.baseline, tiles), reference

    def apply_registry(self, entries: list[CameraRegistryEntry]) -> None:
        def cfg_getter() -> CameraHealthConfig:
            return self.config().defaults.camera_health

        wanted = {e.id: e for e in entries if e.enabled}
        for camera_id in list(self.workers):
            w = self.workers[camera_id]
            new = wanted.get(camera_id)
            if (
                new is None
                or new.sub_path != w.entry.sub_path
                or new.main_path != w.entry.main_path
            ):
                w.reader.stop()
                del self.workers[camera_id]
                log.info("camera_health_stopped", camera=camera_id)
            elif new != w.entry:
                if new.baseline != w.entry.baseline:
                    w.baseline, w.reference["ref"] = self._load_baseline(new)
                w.entry = new
        for camera_id, entry in wanted.items():
            if camera_id in self.workers:
                continue
            path = entry.sub_path or entry.main_path
            baseline, reference = self._load_baseline(entry)
            holder: dict[str, Reference | None] = {"ref": reference}
            reader = CameraReader(
                camera_id,
                f"{self.rtsp}/{path}",
                cfg_getter,
                _ref_getter(holder),
                self.detector,
            )
            cfg = self.config().defaults.camera_health
            self.workers[camera_id] = CameraWorker(
                entry=entry,
                reader=reader,
                debouncer=IssueDebouncer(cfg.raise_after_checks, cfg.clear_after_checks),
                started_mono=time.monotonic(),
                baseline=baseline,
                reference=holder,
            )
            reader.start()
            log.info("camera_health_started", camera=camera_id, path=path)

    async def watch_registry(self, stop: asyncio.Event) -> None:
        pubsub = self.valkey.pubsub()
        await pubsub.subscribe(REGISTRY_CHANNEL, CONFIG_CHANNEL)
        try:
            next_poll = 0.0
            while not stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                channel = message.get("channel") if message else None
                if channel in (CONFIG_CHANNEL, CONFIG_CHANNEL.encode()):
                    try:
                        self.reload_config()
                        log.info("config_reloaded")
                    except Exception as exc:  # невалидный конфиг — продолжаем со старым
                        log.warning("config_reload_failed", error=str(exc))
                    continue
                if message is not None or time.monotonic() >= next_poll:
                    try:
                        self.apply_registry(await self.load_registry())
                    except Exception as exc:
                        log.warning("registry_load_failed", error=str(exc))
                    next_poll = time.monotonic() + REGISTRY_POLL_S
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(REGISTRY_CHANNEL, CONFIG_CHANNEL)
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    # ------------------------------------------------------------ оценка и публикация

    async def assess_all(self) -> int:
        cfg = self.config().defaults.camera_health
        now = time.monotonic()
        published = 0
        for camera_id, w in list(self.workers.items()):
            snap = w.reader.snapshot()
            observed, values = observe(
                snap,
                now,
                cfg,
                w.baseline,
                w.entry.expected_fps,
                w.prev_reconnects,
                w.prev_decode_errors,
                w.started_mono,
            )
            active = w.debouncer.update(observed)
            health = build_health(camera_id, snap, active, values, w.entry.expected_fps)
            self._metrics(
                camera_id,
                health,
                snap.reconnects_total - w.prev_reconnects,
                snap.decode_errors_total - w.prev_decode_errors,
            )
            w.prev_reconnects = snap.reconnects_total
            w.prev_decode_errors = snap.decode_errors_total
            data = health.model_dump_json()
            await self.valkey.xadd(
                HEALTH_STREAM, {"json": data}, maxlen=STREAM_MAXLEN, approximate=True
            )
            await self.valkey.set(
                LATEST_KEY.format(camera=camera_id), data, ex=int(cfg.pipeline_silence_s * 4)
            )
            published += 1
        self.last_publish_mono = now
        return published

    def _metrics(
        self, camera_id: str, health: CameraHealth, new_reconnects: int, new_errors: int
    ) -> None:
        self.m_fps_in.labels(camera=camera_id).set(health.fps_in)
        self.m_fps_processed.labels(camera=camera_id).set(health.fps_processed)
        self.m_frozen.labels(camera=camera_id).set(1 if CameraIssue.FROZEN in health.issues else 0)
        if health.sharpness_ratio is not None:
            self.m_sharpness.labels(camera=camera_id).set(health.sharpness_ratio)
        if health.view_shift_score is not None:
            self.m_view_shift.labels(camera=camera_id).set(health.view_shift_score)
        if new_reconnects > 0:
            self.m_reconnects.labels(camera=camera_id).inc(new_reconnects)
        if new_errors > 0:
            self.m_decode_errors.labels(camera=camera_id).inc(new_errors)

    async def run(self, stop: asyncio.Event) -> None:
        watcher = asyncio.create_task(self.watch_registry(stop))
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    await self.assess_all()
                except (
                    Exception
                ) as exc:  # Valkey недоступен и т.п. — продолжаем, core-api увидит тишину
                    log.warning("camera_health_publish_failed", error=str(exc))
                interval = self.config().defaults.camera_health.check_interval_s
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=max(0.1, interval - (time.monotonic() - started))
                    )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            for w in self.workers.values():
                w.reader.stop()
