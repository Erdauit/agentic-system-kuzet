"""Сессия ONNX Runtime с выбором исполнителя.

- onnxruntime (разработка, CI): CPUExecutionProvider;
- tensorrt (объект): TensorrtExecutionProvider с FP16 и кэшем движков, затем CUDA и CPU как
  запасные. Движок строится на целевой машине при первом запуске (SPEC.md §5.7); кэш лежит в
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
        providers: list[Any]
        if runtime == "tensorrt":
            if engines_root is None:
                raise ValueError("для TensorRT нужен каталог кэша движков")
            cache = engine_cache_dir(engines_root, gpu_key, artifact)
            cache.mkdir(parents=True, exist_ok=True)
            providers = [
                (
                    "TensorrtExecutionProvider",
                    {
                        "trt_fp16_enable": True,
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": str(cache),
                    },
                ),
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ]
        else:
            providers = ["CPUExecutionProvider"]
        available = set(ort.get_available_providers())
        chosen = [p for p in providers if (p[0] if isinstance(p, tuple) else p) in available]
        self.session = ort.InferenceSession(str(artifact.path), sess_options=opts, providers=chosen)
        self.providers = self.session.get_providers()
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
