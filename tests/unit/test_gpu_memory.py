"""Лимиты провайдеров и замеры VRAM: без настоящей видеокарты и весов."""

from __future__ import annotations

import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from shared.config import QoimaConfig
from shared.config.models import GpuMemoryConfig
from vision_pipeline.bench import models as bench_models
from vision_pipeline.bench.memory import VramPeak
from vision_pipeline.inference.runner import CPU, CUDA, TRT, choose_providers, engine_cache_dir


def test_provider_limits_are_bytes_and_cpu_remains_available(tmp_path: Path) -> None:
    memory = GpuMemoryConfig(cuda_arena_limit_mb=768, trt_workspace_mb=256)
    providers = choose_providers("auto", {TRT, CUDA, CPU}, tmp_path, memory)
    assert providers[0][0] == TRT
    assert providers[0][1]["trt_max_workspace_size"] == 256 * 1024**2
    assert providers[1] == (
        CUDA,
        {"gpu_mem_limit": 768 * 1024**2, "arena_extend_strategy": "kSameAsRequested"},
    )
    assert providers[-1] == CPU
    assert choose_providers("onnxruntime", {TRT, CUDA, CPU}, tmp_path, memory) == [CPU]
    assert choose_providers("auto", {CPU}, tmp_path, memory) == [CPU]


def test_unmeasured_limits_are_not_invented(tmp_path: Path) -> None:
    memory = GpuMemoryConfig()
    providers = choose_providers("auto", {TRT, CUDA, CPU}, tmp_path, memory)
    assert "trt_max_workspace_size" not in providers[0][1]
    assert "gpu_mem_limit" not in providers[1][1]
    assert providers[1][1]["arena_extend_strategy"] == "kSameAsRequested"


@pytest.mark.parametrize(
    "values",
    [
        {"cuda_arena_limit_mb": 0},
        {"cuda_arena_limit_mb": -1},
        {"trt_workspace_mb": 0},
        {"trt_workspace_mb": -1},
        {"cuda_arena_extend_strategy": "unbounded"},
    ],
)
def test_invalid_gpu_memory_configuration_is_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GpuMemoryConfig.model_validate(values)


def test_workspace_change_does_not_reuse_old_engine(tmp_path: Path) -> None:
    artifact: Any = SimpleNamespace(version="v1")
    original = engine_cache_dir(tmp_path, "gpu", artifact)
    small = engine_cache_dir(tmp_path, "gpu", artifact, GpuMemoryConfig(trt_workspace_mb=64))
    large = engine_cache_dir(tmp_path, "gpu", artifact, GpuMemoryConfig(trt_workspace_mb=128))
    assert len({original, small, large}) == 3


def test_model_passes_memory_limits_into_ort_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import onnxruntime as ort

    from vision_pipeline.inference.runner import OnnxModel

    calls = []

    def session(path: str, *, sess_options: Any, providers: Any) -> Any:
        calls.append(providers)
        return SimpleNamespace(
            get_providers=lambda: [p[0] if isinstance(p, tuple) else p for p in providers]
        )

    monkeypatch.setattr(ort, "get_available_providers", lambda: [TRT, CUDA, CPU])
    monkeypatch.setattr(ort, "InferenceSession", session)
    artifact: Any = SimpleNamespace(
        path=tmp_path / "model.onnx",
        version="v1",
        runtime=SimpleNamespace(input_name="input", outputs=["out"]),
    )
    OnnxModel(
        artifact,
        "auto",
        1,
        tmp_path,
        "gpu",
        gpu_memory=GpuMemoryConfig(cuda_arena_limit_mb=128, trt_workspace_mb=64),
    )
    assert calls[0][0][1]["trt_max_workspace_size"] == 64 * 1024**2
    assert calls[0][1][1]["gpu_mem_limit"] == 128 * 1024**2
    assert Path(calls[0][0][1]["trt_engine_cache_path"]).name == "workspace-64mb"


def test_peak_observes_memory_during_work_and_stops() -> None:
    busy = threading.Event()
    observed = threading.Event()

    def read() -> float:
        if busy.is_set():
            observed.set()
            return 150.25
        return 100.0

    with VramPeak(read, baseline_mb=100.0, interval_s=0.001) as peak:
        busy.set()
        assert observed.wait(2), "фоновый замер не увидел пик внутри инференса"
        busy.clear()
    assert peak.mb == 51  # округление вверх, а не потеря долей МиБ
    assert not any(t.name == "qoima-vram-sampler" for t in threading.enumerate())


@pytest.mark.parametrize("reading", [None, -1.0, 90.0, float("nan"), float("inf")])
def test_unreliable_memory_reading_is_unknown(reading: float | None) -> None:
    with VramPeak(lambda: reading, baseline_mb=100, interval_s=1) as peak:
        pass
    assert peak.mb is None


def test_memory_probe_failure_does_not_break_benchmark() -> None:
    def unavailable() -> float:
        raise RuntimeError("NVML unavailable")

    with VramPeak(unavailable, baseline_mb=100, interval_s=1) as peak:
        pass
    assert peak.mb is None


def test_peak_thread_stops_when_inference_fails() -> None:
    with (
        pytest.raises(RuntimeError, match="inference failed"),
        VramPeak(lambda: 110, baseline_mb=100, interval_s=0.001),
    ):
        raise RuntimeError("inference failed")
    assert not any(t.name == "qoima-vram-sampler" for t in threading.enumerate())


@pytest.mark.parametrize("gpu", [True, False])
def test_benchmark_measures_each_batch_and_passes_limits(
    monkeypatch: pytest.MonkeyPatch, config: QoimaConfig, tmp_path: Path, gpu: bool
) -> None:
    ticks = count(step=0.001)
    monkeypatch.setattr(bench_models.time, "perf_counter", lambda: next(ticks))
    state = {"used": 100.0}
    memory = GpuMemoryConfig(cuda_arena_limit_mb=768, trt_workspace_mb=256)
    inf = config.defaults.pipeline.inference.model_copy(update={"gpu_memory": memory})
    cfg = config.defaults.bench.model_copy(
        update={"repeats": 1, "model_batches": [1, 4], "model_warmup": 1, "model_iterations": 3}
    )
    artifact = SimpleNamespace(
        version="v1", runtime=SimpleNamespace(dynamic_batch=True, input_height=8, input_width=8)
    )
    monkeypatch.setattr(bench_models, "load_artifact", lambda *_: artifact)

    class Model:
        providers = [CUDA, CPU] if gpu else [CPU]
        fallback_reason = None

        def __init__(self, *_: Any, gpu_memory: GpuMemoryConfig) -> None:
            assert gpu_memory == memory
            state["used"] = 110.0

        def run(self, batch: np.ndarray) -> None:
            state["used"] = 100.0 + batch.shape[0] * 20

    monkeypatch.setattr(bench_models, "OnnxModel", Model)
    results, warnings = bench_models.measure_model(
        tmp_path, "person", inf, cfg, tmp_path, "gpu", lambda: state["used"]
    )
    assert [m.vram_mb for m in results] == ([20, 80] if gpu else [None, None])
    assert not warnings


def test_gpu_without_nvml_reports_unknown_memory(
    monkeypatch: pytest.MonkeyPatch, config: QoimaConfig, tmp_path: Path
) -> None:
    ticks = count(step=0.001)
    monkeypatch.setattr(bench_models.time, "perf_counter", lambda: next(ticks))
    artifact = SimpleNamespace(
        version="v1", runtime=SimpleNamespace(dynamic_batch=True, input_height=8, input_width=8)
    )
    monkeypatch.setattr(bench_models, "load_artifact", lambda *_: artifact)
    monkeypatch.setattr(
        bench_models,
        "OnnxModel",
        lambda *a, **kw: SimpleNamespace(
            providers=[CUDA], fallback_reason=None, run=lambda x: None
        ),
    )
    cfg = config.defaults.bench.model_copy(
        update={"repeats": 1, "model_batches": [1], "model_warmup": 0, "model_iterations": 3}
    )
    results, warnings = bench_models.measure_model(
        tmp_path, "person", config.defaults.pipeline.inference, cfg, tmp_path, "gpu"
    )
    assert results[0].vram_mb is None
    assert any("VRAM" in warning for warning in warnings)
