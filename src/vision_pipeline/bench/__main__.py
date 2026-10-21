"""`python -m vision_pipeline.bench` — бенчмарк узла (запускает `qoima bench` разовым контейнером
из образа vision-pipeline, чтобы мерить в той же среде, что и рабочий пайплайн).

    python -m vision_pipeline.bench --out /var/lib/qoima/bench [--vlm-url http://llm:8000]

Результат — BenchResult в <out>/bench-YYYYMMDD-HHMMSS.json; путь печатается последней строкой.
Hot path не останавливается: текущая нагрузка пишется в live_load (план считает с запасом).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from shared.config import QoimaConfig, load_config
from shared.config.models import BenchConfig
from shared.schemas import BenchResult, CameraRegistryEntry, DecodeBackend, LiveLoad
from shared.telemetry import configure_logging, get_logger
from vision_pipeline.bench.decode import Sample, camera_sample, measure_decode, synthetic_sample
from vision_pipeline.bench.hardware import fingerprint, vram_meter
from vision_pipeline.bench.models import measure_model
from vision_pipeline.bench.vlm import measure_vlm
from vision_pipeline.inference.runner import gpu_key

log = get_logger("bench")

REGISTRY_KEY = "qoima:cameras"
HEALTH_LATEST = "qoima:camera_health:latest:*"


async def _registry_and_load() -> tuple[list[CameraRegistryEntry], LiveLoad | None, list[str]]:
    """Камеры из реестра core-api и нагрузка работающего пайплайна (Valkey)."""
    from shared.storage import valkey

    try:
        client = valkey.create_client()
    except Exception as exc:
        return [], None, [f"Valkey недоступен — камеры и текущая нагрузка не учтены: {exc}"]
    try:
        raw = await client.get(REGISTRY_KEY)
        entries = [CameraRegistryEntry.model_validate(e) for e in json.loads(raw)] if raw else []
        decoded = inferred = 0.0
        alive = 0
        async for key in client.scan_iter(match=HEALTH_LATEST):
            data = await client.get(key)
            if not data:
                continue
            h = json.loads(data)
            if h.get("stream_alive"):
                alive += 1
                decoded += float(h.get("fps_in") or 0)
                inferred += float(h.get("fps_processed") or 0)
        return (
            [e for e in entries if e.enabled],
            LiveLoad(cameras=alive, decoded_fps=decoded, inferred_fps=inferred),
            [],
        )
    except Exception as exc:
        return [], None, [f"реестр камер не прочитан: {exc}"]
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def _samples(
    entries: list[CameraRegistryEntry], rtsp: str, cfg: BenchConfig, use_cameras: bool
) -> tuple[list[Sample], list[str]]:
    samples: dict[tuple[str, int, int], Sample] = {}
    warnings: list[str] = []
    if use_cameras:
        for e in entries:
            path = e.sub_path or e.main_path
            try:
                s = camera_sample(f"{rtsp.rstrip('/')}/{path}", cfg.decode_sample_seconds, e.id)
            except Exception as exc:
                warnings.append(f"{e.id}: образец не записан ({type(exc).__name__}: {exc})")
                continue
            key = (s.codec, s.width, s.height)
            if key in samples:
                samples[key].camera_ids.append(e.id)
            elif s.packets:
                samples[key] = s
    for prof in cfg.synthetic_profiles:
        key = (prof.codec, prof.width, prof.height)
        if key not in samples:
            samples[key] = synthetic_sample(prof, cfg.decode_sample_seconds)
    return list(samples.values()), warnings


def _roi_images(data_root: Path, n: int) -> list[bytes]:
    """4 ROI-кропа для запроса арбитра: из снапшотов камер, иначе синтетика."""
    crops: list[bytes] = []
    for snap in sorted((data_root / "frames").glob("*/baseline.jpg"))[:n]:
        img = cv2.imread(str(snap))
        if img is not None:
            h, w = img.shape[:2]
            crop = img[h // 4 : h // 4 + h // 2, w // 4 : w // 4 + w // 2]
            ok, buf = cv2.imencode(".jpg", cv2.resize(crop, (448, 448)))
            if ok:
                crops.append(buf.tobytes())
    rng = np.random.default_rng(1)
    while len(crops) < n:
        ok, buf = cv2.imencode(".jpg", rng.integers(0, 256, (448, 448, 3), dtype=np.uint8))
        crops.append(buf.tobytes())
    return crops


def run(args: argparse.Namespace, config: QoimaConfig) -> BenchResult:
    d = config.defaults
    cfg = d.bench
    if args.quick:
        cfg = cfg.model_copy(
            update={
                "repeats": 1,
                "decode_seconds": 1.0,
                "decode_sample_seconds": 3.0,
                "max_parallel_decoders": 4,
                "model_iterations": 5,
                "model_warmup": 1,
                "vlm_requests_per_level": 2,
            }
        )
    started = datetime.now(UTC)
    warnings: list[str] = []
    hw = fingerprint()
    entries, live, w = asyncio.run(_registry_and_load()) if not args.no_cameras else ([], None, [])
    warnings += w
    rtsp = os.environ.get("QOIMA_MEDIAMTX_RTSP_URL", "rtsp://mediamtx:8554")
    samples, w = _samples(entries, rtsp, cfg, not args.no_cameras)
    warnings += w
    meter = vram_meter(hw.gpus[0].index) if hw.gpus else None
    decode = []
    backends: list[tuple[DecodeBackend, int | None]] = [(DecodeBackend.CPU, None)]
    if hw.gpus and hw.gpus[0].nvdec_available:
        backends.insert(0, (DecodeBackend.NVDEC, hw.gpus[0].index))
    elif hw.gpus:
        warnings.append("GPU есть, но FFmpeg не видит CUDA (NVDEC) — декодирование только на CPU")
    for s in samples:
        for backend, gpu in backends:
            m, w = measure_decode(s, backend, gpu, cfg, d.pipeline.motion, meter)
            warnings += w
            if m is not None:
                decode.append(m)
    inf = d.pipeline.inference
    data_root = Path(os.environ.get("QOIMA_DATA_DIR", str(d.storage.data_root)))
    models_dir = Path(args.models_dir) if args.models_dir else inf.models_dir
    key = gpu_key() if inf.runtime != "onnxruntime" else "cpu"
    models = []
    for name in [inf.person_model, *([inf.pose_model] if inf.pose_model else [])]:
        ms, w = measure_model(
            models_dir, name, inf, cfg, data_root / "models" / "engines", key, meter
        )
        models += ms
        warnings += w
    vlm: list[Any] = []
    if args.vlm_url:
        vlm, w = asyncio.run(
            measure_vlm(args.vlm_url, args.vlm_model, _roi_images(data_root, d.vlm.roi_frames), cfg)
        )
        warnings += w
    else:
        warnings.append("VLM не измерялся (--vlm-url не задан; сервис llm — Фаза 4)")
    finished = datetime.now(UTC)
    return BenchResult(
        id=f"bench-{started:%Y%m%d-%H%M%S}",
        started_at=started,
        finished_at=finished,
        hardware=hw,
        repeats=cfg.repeats,
        decode=decode,
        models=models,
        vlm=vlm,
        live_load=live,
        warnings=warnings,
    )


def save(result: BenchResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{result.id}.json"
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(result.model_dump_json(indent=2))
    Path(tmp).replace(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vision_pipeline.bench")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--vlm-url", default=None)
    parser.add_argument("--vlm-model", default=None)
    parser.add_argument("--models-dir", default=None)
    parser.add_argument("--no-cameras", action="store_true", help="только синтетические образцы")
    parser.add_argument("--quick", action="store_true", help="короткий прогон (разработка, тесты)")
    args = parser.parse_args(argv)
    configure_logging("bench", os.environ.get("QOIMA_LOG_LEVEL", "INFO"))
    result = run(args, load_config())
    path = save(result, args.out)
    print(path, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
