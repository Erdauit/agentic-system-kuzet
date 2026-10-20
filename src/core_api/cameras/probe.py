"""Зондирование RTSP-потоков (аналог ffprobe на PyAV): кодек, разрешение, FPS, битрейт, GOP,
снапшот и базовая линия качества. Выбор main и substream среди кандидатов.

PyAV — блокирующая библиотека: вызовы выполняются в пуле потоков с ограничением параллелизма
(камеры и NVR плохо переносят много одновременных RTSP-сессий).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import av
import numpy as np

from core_api.cameras.discovery import strip_credentials
from core_api.cameras.onvif import Credentials
from shared.config.models import ProbeConfig
from shared.media.quality import laplacian_variance, mean_luma, noise_sigma, to_analysis_size
from shared.schemas import (
    CandidateProbe,
    ProbeReport,
    StreamCandidate,
    StreamProbe,
    StreamRole,
    VideoCodec,
)

_CODECS = {"h264": VideoCodec.H264, "hevc": VideoCodec.H265, "mjpeg": VideoCodec.MJPEG}


def with_credentials(url: str, creds: Credentials | None) -> str:
    """URL с логином и паролем — только для передачи в FFmpeg/MediaMTX, никогда в лог и БД."""
    if creds is None:
        return url
    parts = urlsplit(strip_credentials(url))
    host = parts.hostname or ""
    netloc = f"{quote(creds.username, safe='')}:{quote(creds.password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass
class ProbeResult:
    probe: StreamProbe
    snapshot_jpeg: bytes | None
    gray: np.ndarray[Any, np.dtype[np.uint8]] | None


class ProbeError(Exception):
    pass


def _encode_jpeg(frame: av.VideoFrame) -> bytes:
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width = frame.width
    ctx.height = frame.height
    ctx.pix_fmt = "yuvj420p"
    ctx.time_base = Fraction(1, 25)
    out = frame.reformat(format="yuvj420p")
    packets = [*ctx.encode(out), *ctx.encode(None)]
    return b"".join(bytes(p) for p in packets)


def probe_stream(
    url: str, creds: Credentials | None, role: StreamRole, cfg: ProbeConfig
) -> ProbeResult:
    """Блокирующее зондирование одного потока."""
    timeout_us = str(int(cfg.timeout_s * 1_000_000))
    options = {"rtsp_transport": "tcp", "timeout": timeout_us}
    try:
        container = av.open(
            with_credentials(url, creds), options=options, timeout=(cfg.timeout_s, cfg.timeout_s)
        )
    except av.error.FFmpegError as exc:
        raise ProbeError(_describe(exc)) from exc
    try:
        if not container.streams.video:
            raise ProbeError("в потоке нет видео")
        stream = container.streams.video[0]
        codec_name = stream.codec_context.name
        width, height = stream.codec_context.width, stream.codec_context.height
        tb = float(stream.time_base) if stream.time_base else 1 / 90000
        started = time.monotonic()
        pts: list[float] = []
        key_pts: list[float] = []
        total_bytes = 0
        snapshot: bytes | None = None
        gray: np.ndarray[Any, np.dtype[np.uint8]] | None = None
        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            total_bytes += packet.size
            if packet.pts is not None:
                pts.append(packet.pts * tb)
                if packet.is_keyframe:
                    key_pts.append(packet.pts * tb)
            if packet.is_keyframe and snapshot is None:
                for frame in packet.decode():
                    width, height = frame.width, frame.height
                    snapshot = _encode_jpeg(frame)
                    gray = np.asarray(frame.to_ndarray(format="gray"), dtype=np.uint8)
                    break
            if time.monotonic() - started > cfg.read_seconds:
                break
    except av.error.FFmpegError as exc:
        raise ProbeError(_describe(exc)) from exc
    finally:
        container.close()

    if len(pts) < 2 or not width or not height:
        raise ProbeError("поток открылся, но кадры не поступают")
    span = max(pts) - min(pts)
    fps = (len(pts) - 1) / span if span > 0 else float(stream.average_rate or 0)
    gop = None
    if len(key_pts) >= 2:
        gop = (key_pts[-1] - key_pts[0]) / (len(key_pts) - 1)
    probe = StreamProbe(
        role=role,
        url=strip_credentials(url),
        codec=_CODECS.get(codec_name, VideoCodec.OTHER),
        width=width,
        height=height,
        fps=round(min(max(fps, 0.1), 240), 2),
        bitrate_kbps=int(total_bytes * 8 / span / 1000) if span > 0 else None,
        gop_seconds=round(gop, 2) if gop and gop > 0 else None,
        probed_at=datetime.now(UTC),
    )
    return ProbeResult(probe=probe, snapshot_jpeg=snapshot, gray=gray)


def _describe(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered:
        return "камера отклонила логин или пароль (401)"
    if "404" in text or "not found" in lowered:
        return "по этому адресу потока нет (404)"
    if "timed out" in lowered or "timeout" in lowered:
        return "камера не ответила вовремя"
    if "refused" in lowered:
        return "соединение отклонено (порт закрыт)"
    return text[:300]


def baseline_from_gray(
    gray: np.ndarray[Any, np.dtype[np.uint8]], analysis_width: int
) -> tuple[float, float, float]:
    """(резкость, яркость, шум) на кадре того же размера, что и в Camera Health."""
    small = to_analysis_size(gray, analysis_width)
    return laplacian_variance(small), mean_luma(small), noise_sigma(small)


# ---------------------------------------------------------------- выбор main / sub


@dataclass
class ProbedCandidate:
    candidate: StreamCandidate
    result: ProbeResult | None
    error: str | None


def choose_streams(
    host: str, channel: int, probed: list[ProbedCandidate], substream_max_height: int
) -> tuple[ProbeReport, ProbeResult | None, ProbeResult | None]:
    """main — самое большое разрешение; sub — самое маленькое ≤ substream_max_height (H.264 в
    приоритете: его понимают браузеры для live-просмотра). Если поток один — он и main, и sub,
    с предупреждением."""
    ok = [p for p in probed if p.result is not None]
    warnings: list[str] = []
    main_r: ProbeResult | None = None
    sub_r: ProbeResult | None = None
    if ok:
        by_area = sorted(
            ok, key=lambda p: p.result.probe.width * p.result.probe.height if p.result else 0
        )
        main_r = by_area[-1].result
        subs = [
            p
            for p in by_area
            if p.result is not None
            and p.result.probe.height <= substream_max_height
            and main_r is not None
            and p.result.probe.url != main_r.probe.url
        ]
        subs.sort(
            key=lambda p: (
                p.result.probe.codec is not VideoCodec.H264 if p.result else True,
                p.result.probe.width * p.result.probe.height if p.result else 0,
            )
        )
        if subs:
            sub_r = subs[0].result
        elif main_r is not None and main_r.probe.height <= substream_max_height:
            sub_r = main_r
            warnings.append(
                "отдельный substream не найден — анализ идёт по основному потоку (≤ 720p)"
            )
        else:
            warnings.append(
                "substream ≤ 720p не найден: анализировать основной поток дорого — "
                "включите второй поток в настройках камеры"
            )
        if (
            main_r is not None
            and main_r.probe.codec is VideoCodec.H265
            and (sub_r is None or sub_r.probe.codec is VideoCodec.H265)
        ):
            warnings.append("H.265: live-просмотр в браузере недоступен, нужен H.264 substream")
    main_probe = main_r.probe.model_copy(update={"role": StreamRole.MAIN}) if main_r else None
    sub_probe = sub_r.probe.model_copy(update={"role": StreamRole.SUB}) if sub_r else None
    report = ProbeReport(
        host=host,
        channel=channel,
        main=main_probe,
        sub=sub_probe,
        tried=[
            CandidateProbe(
                url=p.candidate.url,
                ok=p.result is not None,
                error=p.error,
                probe=p.result.probe if p.result else None,
            )
            for p in probed
        ],
        warnings=warnings,
        probed_at=datetime.now(UTC),
    )
    return report, main_r, sub_r


async def probe_candidates(
    candidates: list[StreamCandidate],
    creds: Credentials | None,
    cfg: ProbeConfig,
    semaphore: asyncio.Semaphore,
) -> list[ProbedCandidate]:
    async def one(c: StreamCandidate) -> ProbedCandidate:
        role = c.role_hint or StreamRole.MAIN
        async with semaphore:
            try:
                result = await asyncio.to_thread(probe_stream, c.url, creds, role, cfg)
            except ProbeError as exc:
                return ProbedCandidate(c, None, str(exc))
        return ProbedCandidate(c, result, None)

    return list(await asyncio.gather(*(one(c) for c in candidates)))
