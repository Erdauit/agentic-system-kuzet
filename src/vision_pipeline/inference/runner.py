"""Сессия ONNX Runtime с выбором исполнителя.

- onnxruntime (разработка, CI): CPUExecutionProvider;
- auto / tensorrt (объект, образ GPU): TensorrtExecutionProvider с FP16 и кэшем движков, затем
  CUDA и CPU как запасные; если GPU-провайдеры не стартовали — CPU с причиной в fallback_reason. Движок строится на целевой машине при первом запуске (SPEC.md §5.7); кэш лежит в
  models/engines/<ключ> и привязан к GPU, драйверу и версии TensorRT — при их смене строится заново.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from vision_pipeline.inference.registry import ModelArtifact


def engine_cache_dir(engines_root: Path, gpu_key: str, artifact: ModelArtifact) -> Path:
    return engines_root / gpu_key / artifact.version


def gpu_key() -> str:
    """Ключ кэша движков TensorRT: модель GPU, версия драйвера и onnxruntime (в его GPU-сборку
    входит конкретная версия TensorRT). Без nvidia-smi — «cpu»."""
    import onnxruntime as ort

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "cpu"
    name, driver = (part.strip() for part in out.split(",", 1))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return f"{slug}-drv{driver}-ort{ort.__version__}"


TRT = "TensorrtExecutionProvider"
CUDA = "CUDAExecutionProvider"
CPU = "CPUExecutionProvider"


def choose_providers(
    runtime: str, available: set[str], engine_cache: Path | None
) -> list[str | tuple[str, dict[str, Any]]]:
    """auto — TensorRT, если он есть в сборке onnxruntime, иначе CUDA, иначе CPU;
    tensorrt — то же, но без TensorRT это ошибка конфигурации (сообщается в лог и health);
    onnxruntime — только CPU (разработка, CI)."""
    if runtime == "onnxruntime":
        return [CPU]
    chosen: list[str | tuple[str, dict[str, Any]]] = []
    if TRT in available and engine_cache is not None:
        chosen.append(
            (
                TRT,
                {
                    "trt_fp16_enable": True,
                    "trt_engine_cache_enable": True,
                    "trt_engine_cache_path": str(engine_cache),
                },
            )
        )
    if CUDA in available:
        chosen.append(CUDA)
    chosen.append(CPU)
    return chosen


class OnnxModel:
    def __init__(
        self,
        artifact: ModelArtifact,
        runtime: str,
        threads: int,
        engines_root: Path | None = None,
        gpu_key: str = "cpu",
    ) -> None:
        import onnxruntime as ort

        self.artifact = artifact
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        available = set(ort.get_available_providers())
        cache = None
        if engines_root is not None and runtime != "onnxruntime":
            cache = engine_cache_dir(engines_root, gpu_key, artifact)
            cache.mkdir(parents=True, exist_ok=True)
        chosen = choose_providers(runtime, available, cache)
        self.fallback_reason: str | None = None
        if runtime == "tensorrt" and TRT not in available:
            self.fallback_reason = "в сборке onnxruntime нет TensorRT (нужен образ GPU)"
        try:
            self.session = ort.InferenceSession(
                str(artifact.path), sess_options=opts, providers=chosen
            )
        except Exception as exc:  # GPU недоступен (нет драйвера, не та видеокарта) — честно на CPU
            if chosen == [CPU]:
                raise
            self.fallback_reason = f"GPU-провайдеры не запустились: {exc}"[:300]
            self.session = ort.InferenceSession(
                str(artifact.path), sess_options=opts, providers=[CPU]
            )
        self.providers = self.session.get_providers()
        wanted_gpu = {(c[0] if isinstance(c, tuple) else c) for c in chosen} & {TRT, CUDA}
        if wanted_gpu and not wanted_gpu & set(self.providers) and self.fallback_reason is None:
            # onnxruntime молча переходит на CPU, если провайдер не загрузился (нет libcuda, не та
            # видеокарта) — говорим об этом явно: «Состояние системы» и бенчмарк покажут причину.
            self.fallback_reason = (
                "GPU-провайдеры onnxruntime не загрузились (нет драйвера NVIDIA или GPU не "
                "передан в контейнер) — модель работает на CPU"
            )
        self.input_name = artifact.runtime.input_name
        self.outputs = artifact.runtime.outputs
        self._lock = threading.Lock()

    def run(self, batch: NDArray[np.float32]) -> dict[str, NDArray[np.float32]]:
        """Выполняет батч. Если модель экспортирована с фиксированным батчем 1 — по одному."""
        if self.artifact.runtime.dynamic_batch or batch.shape[0] == 1:
            with self._lock:
                outs = self.session.run(self.outputs, {self.input_name: batch})
            return {name: np.asarray(o) for name, o in zip(self.outputs, outs, strict=True)}
        parts = [self.run(batch[i : i + 1]) for i in range(batch.shape[0])]
        return {name: np.concatenate([p[name] for p in parts]) for name in self.outputs}
