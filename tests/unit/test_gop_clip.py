"""GOP-буфер и клип без перекодирования: лимиты, выравнивание по ключевому кадру, MP4 читается,
ключевые кадры декодируются в нужные моменты."""

from __future__ import annotations

from pathlib import Path

import av

from tests.unit.media import fill, h264_stream
from vision_pipeline.ingest.clip import decode_at, encode_jpeg, write_mp4
from vision_pipeline.ingest.gop import GopBuffer, StreamInfo

T0 = 1_700_000_000.0


def test_buffer_trims_by_time_and_bytes() -> None:
    info = StreamInfo("h264", 64, 48, b"")
    buf = GopBuffer(max_seconds=5.0, max_bytes=10_000)
    buf.start_segment(info)
    for i in range(100):  # 10 с при 10 к/с, пакеты по 50 байт
        buf.append(T0 + i / 10, T0 + i / 10, i % 10 == 0, b"x" * 50)
    w = buf.window(T0, T0 + 100)
    assert w is not None
    assert w.ends_at - w.packets[0].t <= 5.0  # старше 5 с — вытеснено
    assert w.packets[0].keyframe  # окно начинается с ключевого кадра

    small = GopBuffer(max_seconds=60.0, max_bytes=1_000)
    small.start_segment(info)
    for i in range(100):
        small.append(T0 + i / 10, T0 + i / 10, i % 10 == 0, b"x" * 50)
    assert small.size_bytes <= 1_000


def test_window_starts_at_keyframe_before_t0() -> None:
    info = StreamInfo("h264", 64, 48, b"")
    buf = GopBuffer(60.0, 10**7)
    buf.start_segment(info)
    for i in range(50):
        buf.append(T0 + i / 10, T0 + i / 10, i % 10 == 0, bytes([i]))
    w = buf.window(T0 + 2.35, T0 + 3.0)
    assert w is not None
    assert w.packets[0].t == T0 + 2.0 and w.packets[0].keyframe
    assert w.ends_at <= T0 + 3.0
    last = buf.last_gop()
    assert last is not None and last.packets[0].t == T0 + 4.0 and len(last.packets) == 10


def test_codec_change_does_not_mix_segments() -> None:
    buf = GopBuffer(60.0, 10**7)
    buf.start_segment(StreamInfo("h264", 64, 48, b""))
    for i in range(20):
        buf.append(T0 + i / 10, T0 + i / 10, i % 10 == 0, b"a")
    buf.start_segment(StreamInfo("h264", 128, 96, b""))  # камера сменила разрешение
    for i in range(20, 40):
        buf.append(T0 + i / 10, T0 + i / 10, i % 10 == 0, b"b")
    w = buf.window(T0, T0 + 10)
    assert w is not None and w.info.width == 128
    assert all(p.data == b"b" for p in w.packets)


def test_clip_from_middle_gop_is_playable_mp4(tmp_path: Path) -> None:
    info, packets = h264_stream(frames=40, fps=10, gop=10)
    assert info.extradata.startswith((b"\x00\x00\x00\x01", b"\x00\x00\x01"))
    buf = GopBuffer(60.0, 10**7)
    fill(buf, info, packets, T0)
    # Окно со второй секунды: первый ключевой кадр без SPS/PPS — их добавит write_mp4.
    w = buf.window(T0 + 1.5, T0 + 3.45)
    assert w is not None and w.packets[0].t == T0 + 1.0
    path = tmp_path / "clip.mp4"
    write_mp4(w, path)
    head = path.read_bytes()[:64]
    assert head.find(b"moov") < head.find(b"mdat") or b"mdat" not in head  # faststart
    with av.open(str(path)) as clip:
        vs = clip.streams.video[0]
        frames = list(clip.decode(vs))
        assert len(frames) == len(w.packets) == 25
        assert vs.duration is not None
        assert abs(float(vs.duration * vs.time_base) - 2.5) < 0.05
        assert (frames[0].width, frames[0].height) == (64, 48)


def test_decode_at_targets() -> None:
    info, packets = h264_stream(frames=40, fps=10, gop=10)
    buf = GopBuffer(60.0, 10**7)
    fill(buf, info, packets, T0)
    w = buf.window(T0, T0 + 4)
    assert w is not None
    got = decode_at(w, [T0 + 0.5, T0 + 2.05, T0 + 3.2])
    assert [round(t - T0, 2) for t, _ in got] == [0.5, 2.1, 3.2]
    assert decode_at(w, [T0 + 10]) == []  # за пределами окна — ничего
    # Яркость кадра i = 4·i: убеждаемся, что декодирован именно нужный кадр.
    lum = [int(img.mean()) for _, img in got]
    assert abs(lum[0] - 20) <= 3 and abs(lum[1] - 84) <= 3 and abs(lum[2] - 128) <= 3
    assert encode_jpeg(got[0][1]).startswith(b"\xff\xd8")
