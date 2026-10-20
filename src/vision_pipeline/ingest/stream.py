"""Чтение потока камеры из MediaMTX (PyAV) в отдельном потоке ОС, с переподключением.

Каждый пакет получает настенные метки времени (см. ingest/gop.py) и попадает в GOP-буфер;
наследники решают, что ещё делать с пакетом (Camera Health, декодирование для hot path).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import av

from shared.telemetry import get_logger
from vision_pipeline.ingest.gop import GopBuffer, StreamInfo

log = get_logger("stream-reader")

# Сокет RTSP не блокируется дольше 3 с: поток-читатель успевает заметить остановку и
# переподключиться, а «нет потока» всё равно определяет оценщик по stream_timeout_s.
SOCKET_TIMEOUT_US = 3_000_000


class Clock:
    """pts потока → настенное время: смещение фиксируется по первому пакету подключения."""

    def __init__(self) -> None:
        self.offset: float | None = None

    def wall(self, ts: float | None) -> float:
        now = time.time()
        if ts is None:
            return now
        if self.offset is None:
            self.offset = now - ts
        return self.offset + ts


class StreamReader(threading.Thread):
    def __init__(
        self,
        name: str,
        url: str,
        backoff: Callable[[], tuple[float, float]],
        gop: GopBuffer | None,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.url = url
        self.backoff = backoff
        self.gop = gop
        self.stop_event = threading.Event()
        self.clock = Clock()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        delay, _ = self.backoff()
        while not self.stop_event.is_set():
            try:
                self._read_once()
                delay, _ = self.backoff()
            except av.error.FFmpegError as exc:
                log.debug("stream_error", stream=self.name, error=str(exc))
            except Exception:  # поток-читатель не должен умирать
                log.exception("stream_reader_crash", stream=self.name)
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, self.backoff()[1])

    # Хуки наследников --------------------------------------------------------------------

    def on_connect(self, stream: Any) -> None:
        """Вызывается после открытия потока (до первого пакета)."""

    def on_packet(self, packet: Any, t: float, now_mono: float) -> None:
        """Вызывается для каждого непустого пакета; t — настенное время показа."""

    # -------------------------------------------------------------------------------------

    def _read_once(self) -> None:
        options = {"rtsp_transport": "tcp", "timeout": str(SOCKET_TIMEOUT_US)}
        container = av.open(self.url, options=options, timeout=(5.0, 5.0))
        try:
            stream = container.streams.video[0]
            self.clock = Clock()
            ctx = stream.codec_context
            if self.gop is not None:
                self.gop.start_segment(
                    StreamInfo(ctx.name, ctx.width, ctx.height, bytes(ctx.extradata or b""))
                )
            self.on_connect(stream)
            tb = stream.time_base
            for packet in container.demux(stream):
                if self.stop_event.is_set():
                    return
                if packet.size == 0:
                    continue
                pts = float(packet.pts * tb) if packet.pts is not None and tb else None
                dts = float(packet.dts * tb) if packet.dts is not None and tb else pts
                t = self.clock.wall(pts if pts is not None else dts)
                if self.gop is not None:
                    self.gop.append(
                        t,
                        self.clock.wall(dts) if dts is not None else t,
                        bool(packet.is_keyframe),
                        bytes(packet),
                    )
                self.on_packet(packet, t, time.monotonic())
        finally:
            container.close()
