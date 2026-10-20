"""GOP-буфер (SPEC.md §5.2): сжатые пакеты последних N секунд потока в RAM.

Буфер декодированных кадров запрещён: храним байты пакетов H.264/H.265 как есть. Время пакета —
настенное (unix-секунды): при каждом подключении смещение «pts потока → настенное время»
фиксируется по первому пакету, поэтому переподключения не рвут шкалу, а клип события
вырезается по тем же меткам времени, что и событие.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class StreamInfo:
    """Параметры потока одного подключения. extradata — SPS/PPS (Annex B) из SDP."""

    codec: str
    width: int
    height: int
    extradata: bytes


@dataclass(frozen=True)
class BufferedPacket:
    t: float  # время показа, настенные секунды
    dts: float  # время декодирования, настенные секунды
    keyframe: bool
    data: bytes
    segment: int


@dataclass(frozen=True)
class PacketWindow:
    info: StreamInfo
    packets: list[BufferedPacket]  # начинается с ключевого кадра

    @property
    def starts_at(self) -> float:
        return self.packets[0].t

    @property
    def ends_at(self) -> float:
        return self.packets[-1].t


class GopBuffer:
    """Потокобезопасное кольцо пакетов одного потока с лимитом по времени и по байтам."""

    def __init__(self, max_seconds: float, max_bytes: int) -> None:
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self._packets: deque[BufferedPacket] = deque()
        self._segments: dict[int, StreamInfo] = {}
        self._segment = 0
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def start_segment(self, info: StreamInfo) -> int:
        """Новое подключение к потоку. Пакеты прошлых подключений остаются до вытеснения."""
        with self._lock:
            self._segment += 1
            self._segments[self._segment] = info
            return self._segment

    def append(self, t: float, dts: float, keyframe: bool, data: bytes) -> None:
        with self._lock:
            if self._segment == 0:
                raise RuntimeError("append до start_segment")
            self._packets.append(BufferedPacket(t, dts, keyframe, data, self._segment))
            self._bytes += len(data)
            newest = self._packets[-1].dts
            while self._packets and (
                newest - self._packets[0].dts > self.max_seconds or self._bytes > self.max_bytes
            ):
                self._bytes -= len(self._packets.popleft().data)
            live = {p.segment for p in self._packets} | {self._segment}
            for seg in [s for s in self._segments if s not in live]:
                del self._segments[seg]

    def window(self, t0: float, t1: float) -> PacketWindow | None:
        """Пакеты, покрывающие [t0, t1], начиная с ключевого кадра не позже t0.

        Берутся только подключения с теми же параметрами потока, что у последнего пакета окна:
        смена кодека или разрешения посреди клипа не даёт склеить нечитаемый файл.
        """
        with self._lock:
            packets = [p for p in self._packets if p.dts <= t1]
            segments = dict(self._segments)
        if not packets:
            return None
        info = segments[packets[-1].segment]
        same = [p for p in packets if segments.get(p.segment) == info]
        start = None
        for i, p in enumerate(same):
            if p.keyframe and (p.t <= t0 or start is None):
                start = i
            if p.t > t0 and start is not None:
                break
        if start is None:
            return None
        chosen = same[start:]
        return PacketWindow(info, chosen) if chosen else None

    def last_gop(self) -> PacketWindow | None:
        """Пакеты от последнего ключевого кадра — кадр «сейчас» для самотеста и снапшотов."""
        with self._lock:
            packets = list(self._packets)
            segments = dict(self._segments)
        for i in range(len(packets) - 1, -1, -1):
            if packets[i].keyframe:
                seg = packets[i].segment
                chosen = [p for p in packets[i:] if p.segment == seg]
                return PacketWindow(segments[seg], chosen)
        return None
