"""Точка входа vision-pipeline.

Фаза 1: Camera Health для всех камер реестра (SPEC.md §5.9). Hot path (декодирование NVDEC,
инференс, трекинг, FSM событий) — Фаза 2.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from prometheus_client import CollectorRegistry

from shared.config import QoimaConfig, load_config
from shared.storage import valkey
from shared.telemetry import configure_logging
from shared.telemetry.checks import gpu_check, valkey_check
from shared.telemetry.runner import serve
from shared.telemetry.service import CheckResult, ReadinessCheck
from vision_pipeline import __version__
from vision_pipeline.health.service import CameraHealthService


async def amain() -> None:
    holder: dict[str, QoimaConfig] = {"cfg": load_config()}

    def config() -> QoimaConfig:
        return holder["cfg"]

    def reload_config() -> None:
        holder["cfg"] = load_config()

    registry = CollectorRegistry()
    client = valkey.create_client()
    service = CameraHealthService(
        client,
        config,
        reload_config,
        os.environ.get("QOIMA_MEDIAMTX_RTSP_URL", "rtsp://mediamtx:8554"),
        Path(os.environ.get("QOIMA_DATA_DIR", str(config().defaults.storage.data_root))),
        registry,
    )

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
    await serve(
        service="vision-pipeline",
        version=__version__,
        checks=checks,
        capabilities=["camera_health"],
        notes=["декодирование NVDEC, инференс, трекинг и FSM событий — Фаза 2"],
        work=service.run,
        registry=registry,
    )


def main() -> None:
    configure_logging("vision-pipeline", os.environ.get("QOIMA_LOG_LEVEL", "INFO"))
    asyncio.run(amain())


if __name__ == "__main__":
    main()
