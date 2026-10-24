"""Загрузка моделей hot path из реестра артефактов (inference.models_dir).

Нет артефакта, не сходится sha256 или лицензия — модель не загружается, сервис работает дальше
(Camera Health, GOP-буфер, самотест), а «Состояние системы» показывает причину в notes и
пустые возможности person_detection/fall/red_zone (CLAUDE.md, «Честность кода»).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from prometheus_client import CollectorRegistry, Histogram

from shared.config import QoimaConfig
from shared.telemetry import create_metric, get_logger
from vision_pipeline.hotpath.analyzer import RGB, Perception
from vision_pipeline.inference.batcher import Batcher
from vision_pipeline.inference.detector import PersonDetector
from vision_pipeline.inference.pose import PoseEstimator
from vision_pipeline.inference.registry import ModelUnavailableError, load_artifact
from vision_pipeline.inference.runner import OnnxModel, gpu_key
from vision_pipeline.tracking.tracker import Box, Detection

log = get_logger("models")


@dataclass
class LoadedModels:
    perception: Perception
    notes: list[str]
    batcher: Batcher[RGB, list[Detection]] | None = None

    def close(self) -> None:
        if self.batcher is not None:
            self.batcher.close()


def _histogram(name: str, registry: CollectorRegistry) -> Histogram:
    metric = create_metric(name, registry)
    if not isinstance(metric, Histogram):
        raise TypeError(f"{name} в каталоге должен быть histogram")
    return metric


def load_models(
    config: QoimaConfig, registry: CollectorRegistry, engines_root: Path
) -> LoadedModels:
    pipe = config.defaults.pipeline
    inf = pipe.inference
    notes: list[str] = []
    key = gpu_key() if inf.runtime != "onnxruntime" else "cpu"
    latency = _histogram("inference_latency_seconds", registry)
    batch_size = _histogram("batch_size", registry)
    try:
        art = load_artifact(inf.models_dir, inf.person_model)
        detector = PersonDetector(
            OnnxModel(art, inf.runtime, inf.threads, engines_root, key, gpu_memory=inf.gpu_memory),
            "person",
            inf.person_score_min,
        )
    except (ModelUnavailableError, ValueError) as exc:
        notes.append(f"детектор людей не загружен: {exc}")
        log.warning("person_model_unavailable", error=str(exc))
        return LoadedModels(Perception(None, None), notes)
    log.info("person_model_loaded", version=detector.version, providers=detector.model.providers)
    if detector.model.fallback_reason:
        notes.append(f"детектор людей на CPU: {detector.model.fallback_reason}")

    def on_batch(n: int, seconds: float) -> None:
        batch_size.labels(model=inf.person_model).observe(n)
        latency.labels(model=inf.person_model).observe(seconds)

    batcher: Batcher[RGB, list[Detection]] = Batcher(
        detector.detect, inf.max_batch, pipe.batching.max_queue_delay_ms, on_batch
    )

    def detect(rgb: RGB) -> list[Detection]:
        return batcher.submit(rgb).result(timeout=inf.timeout_s)

    pose_fn = None
    pose_version = None
    if inf.pose_model:
        try:
            pose_art = load_artifact(inf.models_dir, inf.pose_model)
            pose = PoseEstimator(
                OnnxModel(
                    pose_art, inf.runtime, inf.threads, engines_root, key, gpu_memory=inf.gpu_memory
                )
            )
            pose_version = pose.version
            pose_name = inf.pose_model

            def pose_fn(rgb: RGB, boxes: list[Box]) -> list[NDArray[np.float64]]:
                started = time.monotonic()
                out = pose.estimate(rgb, boxes)
                latency.labels(model=pose_name).observe(time.monotonic() - started)
                return out

            log.info("pose_model_loaded", version=pose_version)
        except (ModelUnavailableError, ValueError) as exc:
            notes.append(f"модель позы не загружена (падение — по форме бокса): {exc}")
            log.warning("pose_model_unavailable", error=str(exc))
    return LoadedModels(Perception(detect, pose_fn, detector.version, pose_version), notes, batcher)
