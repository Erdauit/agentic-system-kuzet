"""Клип события из GOP-буфера без перекодирования и ключевые кадры по запросу (SPEC.md §5.2, §7).

Пакеты RTSP приходят в Annex B; SPS/PPS камеры часто передают только в SDP (extradata), поэтому
перед первым ключевым кадром клипа они дописываются в поток — muxer MP4 строит из них avcC/hvcC.
Метки времени клипа пересчитываются из настенного времени буфера (шкала 1/90000).
"""

from __future__ import annotations

from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from numpy.typing import NDArray

from vision_pipeline.ingest.gop import BufferedPacket, PacketWindow

CLIP_TIME_BASE = Fraction(1, 90_000)
ANNEX_B = (b"\x00\x00\x00\x01", b"\x00\x00\x01")


def _first_payload(window: PacketWindow, packet: BufferedPacket) -> bytes:
    extradata = window.info.extradata
    if extradata.startswith(ANNEX_B) and extradata not in packet.data:
        return extradata + packet.data
    return packet.data


def write_mp4(window: PacketWindow, path: Path) -> None:
    """MP4 с moov в начале (faststart: браузер начинает показ до полной загрузки).

    faststart переписывает файл после записи, поэтому вывод — в файл, а не в память.
    """
    packets = window.packets
    if not packets:
        raise ValueError("пустое окно клипа")
    base = packets[0].dts
    container = av.open(str(path), "w", format="mp4", options={"movflags": "+faststart"})
    try:
        stream = container.add_mux_stream(
            window.info.codec, width=window.info.width, height=window.info.height
        )
        stream.time_base = CLIP_TIME_BASE
        last_dts = -1
        ticks = [round((p.dts - base) / CLIP_TIME_BASE) for p in packets]
        for i, p in enumerate(packets):
            dts = max(ticks[i], last_dts + 1)
            pts = max(round((p.t - base) / CLIP_TIME_BASE), dts)
            nxt = ticks[i + 1] if i + 1 < len(packets) else None
            duration = (nxt - dts) if nxt is not None and nxt > dts else (dts - last_dts)
            pkt = av.Packet(_first_payload(window, p) if i == 0 else p.data)
            pkt.dts, pkt.pts, pkt.duration = dts, pts, max(int(duration), 1)
            pkt.time_base = CLIP_TIME_BASE
            pkt.is_keyframe = p.keyframe
            pkt.stream = stream
            container.mux(pkt)
            last_dts = dts
    finally:
        container.close()


def decode_at(window: PacketWindow, targets: list[float]) -> list[tuple[float, NDArray[np.uint8]]]:
    """Первый кадр не раньше каждого момента targets (настенные секунды), RGB.

    Декодирование начинается с ближайшего ключевого кадра перед моментом, а не с начала окна:
    на 4–6 кадров уходит не больше GOP на каждый. Кадры выходят из декодера с задержкой и
    пачками, поэтому лишние копятся в очереди и достаются следующим моментам.
    """
    packets = window.packets
    keys = [i for i, p in enumerate(packets) if p.keyframe]
    if not keys:
        return []
    result: list[tuple[float, NDArray[np.uint8]]] = []
    ctx: Any = None  # VideoCodecContext декодера; в заглушках PyAV у базового класса нет decode
    pending: deque[tuple[float, Any]] = deque()
    pos = 0
    flushed = False
    for target in sorted(targets):
        start = max((i for i in keys if packets[i].t <= target), default=keys[0])
        if ctx is None or start > pos:
            ctx = av.CodecContext.create(window.info.codec, "r")
            if window.info.extradata:
                ctx.extradata = window.info.extradata
            pos, flushed = start, False
            pending.clear()
        found = None
        while found is None:
            while pending and found is None:
                t, frame = pending.popleft()
                if t + 1e-6 >= target:
                    found = (t, np.asarray(frame.to_ndarray(format="rgb24"), dtype=np.uint8))
            if found is not None:
                break
            if pos < len(packets):
                p = packets[pos]
                pkt = av.Packet(_first_payload(window, p) if pos == start else p.data)
                pkt.pts = pkt.dts = round(p.t * 1_000_000)
                pkt.time_base = Fraction(1, 1_000_000)
                pos += 1
                frames = ctx.decode(pkt)
            elif not flushed:
                flushed = True
                frames = ctx.decode(None)
            else:
                break
            pending.extend(((f.pts or 0) / 1_000_000, f) for f in frames)
        if found is None:
            break
        result.append(found)
    return result


def encode_jpeg(rgb: NDArray[np.uint8], quality: int = 85) -> bytes:
    ok, data = cv2.imencode(".jpg", rgb[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("не удалось закодировать JPEG")
    return bytes(data.tobytes())
