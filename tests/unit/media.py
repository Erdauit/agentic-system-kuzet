"""Синтетическое видео для тестов: H.264 Annex B, как его отдаёт RTSP (SPS/PPS — в extradata).

Кодировщик — первый доступный из libopenh264 (образы Qoima) и libx264 (колёса PyAV для
разработки); тестовый ролик не попадает в поставку. Нет ни одного — тест пропускается.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import av
import numpy as np
import pytest

from vision_pipeline.ingest.gop import GopBuffer, StreamInfo

ENCODERS = ("libopenh264", "libx264")


@dataclass(frozen=True)
class EncodedPacket:
    data: bytes
    keyframe: bool


def h264_stream(
    frames: int, fps: int = 10, gop: int = 10, size: tuple[int, int] = (64, 48)
) -> tuple[StreamInfo, list[EncodedPacket]]:
    """Ролик с меняющейся яркостью (кадр i — яркость 4·i), разобранный демультиплексором h264."""
    encoder = next((e for e in ENCODERS if e in av.codecs_available), None)
    if encoder is None:
        pytest.skip("нет кодировщика H.264 (libopenh264/libx264)")
    w, h = size
    raw = io.BytesIO()
    out = av.open(raw, "w", format="h264")
    # Без B-кадров: порядок декодирования = порядку показа, метки времени фикстуры — по номеру.
    options = {"bframes": "0"} if encoder == "libx264" else {}
    stream = out.add_stream(encoder, rate=fps, options=options)
    stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
    stream.codec_context.gop_size = gop
    stream.codec_context.max_b_frames = 0
    for i in range(frames):
        img = np.full((h, w, 3), min(4 * i, 255), np.uint8)
        for p in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            out.mux(p)
    for p in stream.encode():
        out.mux(p)
    out.close()
    raw.seek(0)
    inp = av.open(raw, format="h264")
    vs = inp.streams.video[0]
    info = StreamInfo("h264", w, h, bytes(vs.codec_context.extradata or b""))
    packets = [EncodedPacket(bytes(p), bool(p.is_keyframe)) for p in inp.demux(vs) if p.size]
    inp.close()
    return info, packets


def fill(
    buf: GopBuffer, info: StreamInfo, packets: list[EncodedPacket], t0: float, fps: int = 10
) -> None:
    buf.start_segment(info)
    for i, p in enumerate(packets):
        t = t0 + i / fps
        buf.append(t, t, p.keyframe, p.data)
