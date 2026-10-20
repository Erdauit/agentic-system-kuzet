"""Точка входа vision-pipeline (backend lite).

Camera Health для всех камер реестра (SPEC.md §5.9) + hot path (SPEC.md §5): GOP-буферы,
детектор движения, детектор людей и поза (если артефакты моделей есть), трекер, FSM падения и
красной зоны, события и клипы, тестовые события самотеста/канарейки. Дым и огонь — только с
весами Qoima (сейчас их нет: сценарий не развёрнут).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from prometheus_client import CollectorRegistry

from shared.config import QoimaConfig, load_config
from shared.storage import valkey
from shared.storage.blobstore import LocalFSBlobStore
from shared.telemetry import configure_logging
from shared.telemetry.checks import gpu_check, valkey_check
from shared.telemetry.runner import serve
from shared.telemetry.service import CheckResult, ReadinessCheck
from vision_pipeline import __version__
from vision_pipeline.health.service import CameraHealthService
from vision_pipeline.hotpath.models import load_models
from vision_pipeline.hotpath.service import HotPath


async def amain() -> None:
    holder: dict[str, QoimaConfig] = {"cfg": load_config()}

    def config() -> QoimaConfig:
        return holder["cfg"]

    def reload_config() -> None:
        holder["cfg"] = load_config()

    registry = CollectorRegistry()
    client = valkey.create_client()
    data_root = Path(os.environ.get("QOIMA_DATA_DIR", str(config().defaults.storage.data_root)))
    rtsp = os.environ.get("QOIMA_MEDIAMTX_RTSP_URL", "rtsp://mediamtx:8554")
    models = load_models(config(), registry, data_root / "models" / "engines")
    hotpath = HotPath(
        client, config, LocalFSBlobStore(data_root), rtsp, models.perception, registry
    )
    service = CameraHealthService(
        client, config, reload_config, rtsp, data_root, registry, hotpath=hotpath
    )

    async def work(stop: asyncio.Event) -> None:
        hotpath.bind(asyncio.get_running_loop())  # до запуска читателей камер
        try:
            await asyncio.gather(hotpath.run(stop), service.run(stop))
        finally:
            models.close()

    async def health_loop_alive() -> CheckResult:
        last = service.last_publish_mono
        limit = config().defaults.camera_health.check_interval_s * 3
        if last is None or time.monotonic() - last > limit:
            return CheckResult(False, "цикл Camera Health не публикует снимки")
        return CheckResult(True, f"камер под наблюдением: {len(service.workers)}")

    checks: dict[str, ReadinessCheck] = {
        "valkey": valkey_check(),
        "camera_health": health_loop_alive,
    }
    if os.environ.get("QOIMA_REQUIRE_GPU") == "1":
        checks["gpu"] = gpu_check()
    notes = [
        *models.notes,
        "дым и огонь: нет весов Qoima — сценарий не развёрнут (docs/TODO.md)",
    ]
    await serve(
        service="vision-pipeline",
        version=__version__,
        checks=checks,
        capabilities=["camera_health", *hotpath.capabilities],
        notes=notes,
        work=work,
        registry=registry,
    )


def main() -> None:
    configure_logging("vision-pipeline", os.environ.get("QOIMA_LOG_LEVEL", "INFO"))
    asyncio.run(amain())


if __name__ == "__main__":
    main()
