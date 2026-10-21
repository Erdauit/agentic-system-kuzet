"""Замер моделей: задержка p50/p95 и пропускная способность при разных батчах, память GPU.

Модель запускается тем же OnnxModel и тем же исполнителем (onnxruntime/TensorRT EP), что и в
hot path. Первые вызовы (сборка движка TensorRT, аллокации) — прогрев, они не считаются.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from shared.config.models import BenchConfig, InferenceConfig
from shared.schemas import ModelMeasurement
from shared.telemetry import get_logger
from vision_pipeline.inference.registry import ModelUnavailableError, load_artifact
from vision_pipeline.inference.runner import OnnxModel

log = get_logger("bench-models")


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values), q))


def measure_model(
    models_dir: Path,
    name: str,
    inf: InferenceConfig,
    cfg: BenchConfig,
    engines_root: Path | None,
    gpu_key: str,
    vram_used: Callable[[], float | None] | None = None,
) -> tuple[list[ModelMeasurement], list[str]]:
    try:
        artifact = load_artifact(models_dir, name)
    except ModelUnavailableError as exc:
        return [], [f"модель {name} не измерена: {exc}"]
    before = vram_used() if vram_used else None
    model = OnnxModel(artifact, inf.runtime, inf.threads, engines_root, gpu_key)
    rt = artifact.runtime
    batches = [b for b in cfg.model_batches if b <= inf.max_batch] or [1]
    if not rt.dynamic_batch:
        batches = [1]  # экспорт с фиксированным батчем: батч > 1 — это просто цикл
    rng = np.random.default_rng(0)
    out: list[ModelMeasurement] = []
    vram_mb: int | None = None
    for b in batches:
        x = rng.standard_normal((b, 3, rt.input_height, rt.input_width)).astype(np.float32)
        for _ in range(cfg.model_warmup):
            model.run(x)
        if vram_mb is None and before is not None and vram_used is not None:
            after = vram_used()
            vram_mb = int(after - before) if after is not None else None
        means: list[float] = []
        lat: list[float] = []
        for _ in range(cfg.repeats):
            run: list[float] = []
            for _ in range(cfg.model_iterations):
                started = time.perf_counter()
                model.run(x)
                run.append((time.perf_counter() - started) * 1000)
            lat += run
            means.append(statistics.fmean(run))
        mean = statistics.fmean(means)
        cv = statistics.pstdev(means) / mean if len(means) > 1 and mean > 0 else 0.0
        p50, p95 = _percentile(lat, 50), _percentile(lat, 95)
        m = ModelMeasurement(
            model=name,
            version=artifact.version,
            runtime="tensorrt" if "TensorrtExecutionProvider" in model.providers else "onnxruntime",
            providers=model.providers,
            batch=b,
            latency_p50_ms=round(p50, 3),
            latency_p95_ms=round(max(p95, p50), 3),
            throughput=round(b / (mean / 1000), 2),
            cv=round(cv, 4),
            vram_mb=vram_mb,
        )
        log.info(
            "model_bench", model=name, batch=b, p95_ms=m.latency_p95_ms, throughput=m.throughput
        )
        out.append(m)
    warnings = []
    if model.fallback_reason:
        warnings.append(f"{name}: {model.fallback_reason}; замер на {model.providers[0]}")
    return out, warnings
