"""Замер декодирования (SPEC.md §5.3): сколько кадров/с узел декодирует и уменьшает до миниатюры
детектора движения — ровно то, что hot path делает с каждым кадром потока анализа.

Образцы видео — 10 с с каждой камеры (уникальные кодек и разрешение) через MediaMTX; если камер
нет — синтетический H.264 типовых размеров. Декодеры запускаются потоками в одном процессе, как
в vision-pipeline (поток-читатель на камеру): число потоков удваивается, пока прирост не станет
меньше saturation_gain.
"""

from __future__ import annotations

import io
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import av
import numpy as np
from av.codec.hwaccel import HWAccel, hwdevices_available

from shared.config.models import BenchConfig, DecodeProfile, MotionConfig
from shared.schemas import DecodeBackend, DecodeMeasurement
from shared.telemetry import get_logger

log = get_logger("bench-decode")

ENCODERS = ("libopenh264", "libx264")
ANNEX_B = (b"\x00\x00\x00\x01", b"\x00\x00\x01")
# Типовой битрейт по высоте кадра (как в симуляторе камер): от него зависит цена декодирования.
BITRATE = {360: 400_000, 720: 1_500_000, 1080: 4_000_000}


@dataclass
class Sample:
    codec: str
    width: int
    height: int
    fps: float
    extradata: bytes
    packets: list[bytes]
    source: str  # camera | synthetic
    camera_ids: list[str] = field(default_factory=list)


def nvdec_available() -> bool:
    return "cuda" in hwdevices_available()


def synthetic_sample(profile: DecodeProfile, seconds: float) -> Sample:
    """H.264 с «картинкой камеры»: текстура со сдвигом (движение) и шумом сенсора."""
    encoder = next((e for e in ENCODERS if e in av.codecs_available), None)
    if encoder is None:
        raise RuntimeError("нет кодировщика H.264 (libopenh264/libx264) для синтетического образца")
    w, h, fps = profile.width, profile.height, profile.fps
    raw = io.BytesIO()
    out: Any = av.open(raw, "w", format="h264")
    options = {"bframes": "0"} if encoder == "libx264" else {}
    stream: Any = out.add_stream(encoder, rate=round(fps), options=options)
    stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
    stream.codec_context.gop_size = int(fps * 2)
    stream.codec_context.max_b_frames = 0
    stream.bit_rate = BITRATE.get(h, int(w * h * fps * 0.1))
    rng = np.random.default_rng(42)
    texture = rng.integers(0, 256, size=(h, w * 2, 3), dtype=np.uint8)
    for i in range(int(seconds * fps)):
        shift = (i * 3) % w
        img = texture[:, shift : shift + w].copy()
        img ^= rng.integers(0, 8, size=img.shape, dtype=np.uint8)
        for p in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            out.mux(p)
    for p in stream.encode():
        out.mux(p)
    out.close()
    raw.seek(0)
    inp: Any = av.open(raw, format="h264")
    with inp:
        vs = inp.streams.video[0]
        extradata = bytes(vs.codec_context.extradata or b"")
        packets = [bytes(p) for p in inp.demux(vs) if p.size]
    return Sample("h264", w, h, fps, extradata, packets, "synthetic")


def camera_sample(url: str, seconds: float, camera_id: str) -> Sample:
    """Запись образца из MediaMTX: пакеты начиная с ключевого кадра (декодер начинает с него)."""
    options = {"rtsp_transport": "tcp", "timeout": "5000000"}
    with av.open(url, options=options, timeout=(10.0, 10.0)) as container:
        vs = container.streams.video[0]
        ctx = vs.codec_context
        fps = float(vs.average_rate or vs.guessed_rate or 15)
        packets: list[bytes] = []
        started: float | None = None
        for p in container.demux(vs):
            if not p.size:
                continue
            if started is None:
                if not p.is_keyframe:
                    continue
                started = time.monotonic()
            packets.append(bytes(p))
            if time.monotonic() - started >= seconds:
                break
        return Sample(
            ctx.name,
            ctx.width,
            ctx.height,
            fps,
            bytes(ctx.extradata or b""),
            packets,
            "camera",
            [camera_id],
        )


def _decoder(sample: Sample, backend: DecodeBackend, gpu_index: int | None) -> Any:
    hw = None
    if backend is DecodeBackend.NVDEC:
        hw = HWAccel(device_type="cuda", device=str(gpu_index or 0), allow_software_fallback=False)
    ctx = av.CodecContext.create(sample.codec, "r", hwaccel=hw)
    if sample.extradata:
        ctx.extradata = sample.extradata
    return ctx


def _decode_loop(
    sample: Sample,
    backend: DecodeBackend,
    gpu_index: int | None,
    thumb: tuple[int, int],
    deadline: float,
    counts: list[int],
    slot: int,
    errors: list[str],
) -> None:
    frames = 0
    first = sample.packets[0]
    if sample.extradata.startswith(ANNEX_B) and sample.extradata not in first:
        first = sample.extradata + first
    try:
        while time.monotonic() < deadline:
            ctx = _decoder(sample, backend, gpu_index)
            for i, data in enumerate(sample.packets):
                for frame in ctx.decode(av.Packet(first if i == 0 else data)):
                    frame.reformat(width=thumb[0], height=thumb[1], format="gray")
                    frames += 1
                if time.monotonic() >= deadline:
                    break
    except Exception as exc:  # NVDEC недоступен, кончилась память декодеров и т.п.
        errors.append(f"{type(exc).__name__}: {exc}")
    counts[slot] = frames


def decode_fps(
    sample: Sample,
    backend: DecodeBackend,
    gpu_index: int | None,
    parallel: int,
    seconds: float,
    thumb: tuple[int, int],
) -> tuple[float, list[str]]:
    counts = [0] * parallel
    errors: list[str] = []
    started = time.monotonic()
    deadline = started + seconds
    threads = [
        threading.Thread(
            target=_decode_loop,
            args=(sample, backend, gpu_index, thumb, deadline, counts, i, errors),
            daemon=True,
        )
        for i in range(parallel)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(counts) / max(time.monotonic() - started, 1e-6), errors


def measure_decode(
    sample: Sample,
    backend: DecodeBackend,
    gpu_index: int | None,
    cfg: BenchConfig,
    motion: MotionConfig,
    vram_used: Any = None,
) -> tuple[DecodeMeasurement | None, list[str]]:
    """Поиск насыщения по числу параллельных декодеров, затем повторы на лучшем значении.

    vram_used — функция «занято VRAM, МБ» (NVML) для оценки памяти на один декодер NVDEC.
    """
    thumb = (motion.width, motion.height)
    warnings: list[str] = []
    best_p, best_fps = 1, 0.0
    p = 1
    while p <= cfg.max_parallel_decoders:
        fps, errors = decode_fps(sample, backend, gpu_index, p, cfg.decode_seconds, thumb)
        if errors and fps == 0:
            warnings.append(
                f"{backend.value} {sample.codec} {sample.width}×{sample.height}: {errors[0]}"
            )
            return None, warnings
        log.info(
            "decode_probe",
            backend=backend.value,
            codec=sample.codec,
            width=sample.width,
            parallel=p,
            fps=round(fps, 1),
        )
        if fps < best_fps * (1 + cfg.saturation_gain):
            break
        best_p, best_fps = p, fps
        p *= 2
    runs = [
        decode_fps(sample, backend, gpu_index, best_p, cfg.decode_seconds, thumb)[0]
        for _ in range(cfg.repeats)
    ]
    mean = statistics.fmean(runs)
    cv = statistics.pstdev(runs) / mean if mean > 0 and len(runs) > 1 else 0.0
    per_stream: int | None = None
    if backend is DecodeBackend.NVDEC and vram_used is not None:
        before = vram_used()
        ctxs = [_decoder(sample, backend, gpu_index) for _ in range(4)]
        for ctx in ctxs:  # память выделяется на первом кадре
            for i, data in enumerate(sample.packets[:2]):
                first = (
                    (sample.extradata + data)
                    if i == 0 and sample.extradata.startswith(ANNEX_B)
                    else data
                )
                ctx.decode(av.Packet(first))
        after = vram_used()
        if before is not None and after is not None:
            per_stream = max(int((after - before) / len(ctxs)), 0)
        del ctxs
    return (
        DecodeMeasurement(
            codec=sample.codec,
            width=sample.width,
            height=sample.height,
            source_fps=sample.fps,
            backend=backend,
            gpu_index=gpu_index if backend is DecodeBackend.NVDEC else None,
            parallel_streams=best_p,
            fps_total=round(mean, 2),
            cv=round(cv, 4),
            vram_per_stream_mb=per_stream,
            source="camera" if sample.source == "camera" else "synthetic",
            camera_ids=sample.camera_ids,
        ),
        warnings,
    )
