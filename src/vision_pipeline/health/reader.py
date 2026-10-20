"""Чтение потока камеры из MediaMTX (PyAV) в отдельном потоке ОС.

Для экономии CPU декодируются только I-кадры (обычно раз в 1–3 с): этого достаточно для всех
проверок Camera Health. Полное декодирование (NVDEC) появится в hot path в Фазе 2.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import av
import numpy as np

from shared.config.models import CameraHealthConfig
from shared.telemetry import get_logger
from vision_pipeline.health.analysis import analyze
from vision_pipeline.health.assessor import StatsSnapshot, StreamStats
from vision_pipeline.health.view_shift import Reference, ViewShiftDetector

log = get_logger("camera-reader")

# Сокет RTSP не блокируется дольше 3 с: поток-читатель успевает заметить остановку и
# переподключиться, а «нет потока» всё равно определяет оценщик по stream_timeout_s.
SOCKET_TIMEOUT_US = 3_000_000


class CameraReader(threading.Thread):
    def __init__(
        self,
        camera_id: str,
        url: str,
        cfg: Callable[[], CameraHealthConfig],
        reference: Callable[[], Reference | None],
        detector: ViewShiftDetector,
    ) -> None:
        super().__init__(name=f"camera-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.url = url
        self.cfg = cfg
        self.reference = reference
        self.detector = detector
        self.stats = StreamStats()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
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
                analyzed_times=list(s.frames_analyzed_times),
                view_shift_score=s.view_shift_score,
            )

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        backoff = self.cfg().reconnect_backoff_min_s
        while not self.stop_event.is_set():
            try:
                self._read_once()
                backoff = self.cfg().reconnect_backoff_min_s
            except av.error.FFmpegError as exc:
                log.debug("camera_stream_error", camera=self.camera_id, error=str(exc))
            except Exception:  # поток-читатель не должен умирать
                log.exception("camera_reader_crash", camera=self.camera_id)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, self.cfg().reconnect_backoff_max_s)

    def _read_once(self) -> None:
        options = {"rtsp_transport": "tcp", "timeout": str(SOCKET_TIMEOUT_US)}
        container = av.open(self.url, options=options, timeout=(5.0, 5.0))
        try:
            stream = container.streams.video[0]
            with self.lock:
                if self.stats.connected_once:
                    self.stats.reconnects_total += 1
                self.stats.connected_once = True
            for packet in container.demux(stream):
                if self.stop_event.is_set():
                    return
                if packet.size == 0:
                    continue
                now = time.monotonic()
                with self.lock:
                    self.stats.last_packet_mono = now
                    self.stats.last_packet_wall = datetime.now(UTC)
                    self.stats.packet_times.append(now)
                if packet.is_keyframe:
                    self._analyze(packet, now)
        finally:
            container.close()

    def _analyze(self, packet: Any, now: float) -> None:
        cfg = self.cfg()
        try:
            frames = packet.decode()
        except av.error.InvalidDataError:
            with self.lock:
                self.stats.decode_errors_total += 1
            return
        for frame in frames:
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
            break
