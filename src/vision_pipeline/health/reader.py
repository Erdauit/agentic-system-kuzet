"""Поток анализа камеры (substream): Camera Health + кадры для hot path + GOP-буфер.

Без сценариев hot path декодируются только I-кадры (обычно раз в 1–3 с): этого достаточно для
всех проверок Camera Health. Если на камере включены сценарии, декодируется каждый кадр, а
кадры уходят в hot path (frame_sink); Camera Health по-прежнему смотрит на I-кадры.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import av
import numpy as np

from shared.config.models import CameraHealthConfig
from vision_pipeline.health.analysis import analyze
from vision_pipeline.health.assessor import StatsSnapshot, StreamStats
from vision_pipeline.health.view_shift import Reference, ViewShiftDetector
from vision_pipeline.ingest.gop import GopBuffer
from vision_pipeline.ingest.stream import StreamReader

# Получатель декодированных кадров: (кадр PyAV, настенное время кадра) → кадр ушёл в инференс.
FrameSink = Callable[[av.VideoFrame, float], bool]


class CameraReader(StreamReader):
    def __init__(
        self,
        camera_id: str,
        url: str,
        cfg: Callable[[], CameraHealthConfig],
        reference: Callable[[], Reference | None],
        detector: ViewShiftDetector,
        gop: GopBuffer | None = None,
        frame_sink: FrameSink | None = None,
    ) -> None:
        def backoff() -> tuple[float, float]:
            c = cfg()
            return c.reconnect_backoff_min_s, c.reconnect_backoff_max_s

        super().__init__(f"camera-{camera_id}", url, backoff, gop)
        self.camera_id = camera_id
        self.cfg = cfg
        self.reference = reference
        self.detector = detector
        self.frame_sink = frame_sink
        self.stats = StreamStats()
        self.lock = threading.Lock()
        self._last_view_shift = 0.0

    def snapshot(self) -> StatsSnapshot:
        with self.lock:
            s = self.stats
            return StatsSnapshot(
                connected_once=s.connected_once,
                last_packet_mono=s.last_packet_mono,
                last_packet_wall=s.last_packet_wall,
                packet_times=list(s.packet_times),
                frames=list(s.frames),
                reconnects_total=s.reconnects_total,
                decode_errors_total=s.decode_errors_total,
                # С hot path «обработано» — кадры, ушедшие в инференс; без него — I-кадры health.
                analyzed_times=list(
                    s.inferred_times if self.frame_sink is not None else s.frames_analyzed_times
                ),
                view_shift_score=s.view_shift_score,
            )

    def on_connect(self, stream: Any) -> None:
        if self.frame_sink is not None:
            stream.codec_context.thread_type = "AUTO"
        with self.lock:
            if self.stats.connected_once:
                self.stats.reconnects_total += 1
            self.stats.connected_once = True

    def on_packet(self, packet: Any, t: float, now_mono: float) -> None:
        with self.lock:
            self.stats.last_packet_mono = now_mono
            self.stats.last_packet_wall = datetime.now(UTC)
            self.stats.packet_times.append(now_mono)
        sink = self.frame_sink
        if sink is None and not packet.is_keyframe:
            return
        try:
            frames = packet.decode()
        except av.error.InvalidDataError:
            with self.lock:
                self.stats.decode_errors_total += 1
            return
        for frame in frames:
            if packet.is_keyframe:
                self._analyze(frame, now_mono)
            if sink is not None and sink(frame, self.clock.wall(frame.time)):
                with self.lock:
                    self.stats.inferred_times.append(now_mono)

    def _analyze(self, frame: Any, now: float) -> None:
        cfg = self.cfg()
        gray = np.asarray(frame.to_ndarray(format="gray"), dtype=np.uint8)
        stats, small = analyze(gray, now, cfg.analysis_width, cfg.occlusion_grid)
        score: float | None = None
        if now - self._last_view_shift >= cfg.view_shift_check_interval_s:
            ref = self.reference()
            if ref is not None:
                score = self.detector.score(ref, small)
                self._last_view_shift = now
        with self.lock:
            self.stats.frames.append(stats)
            self.stats.frames_analyzed_times.append(now)
            if score is not None:
                self.stats.view_shift_score = score
