"""Точка входа vision-pipeline.

Фаза 0: процесс поднимает служебный HTTP (/healthz, /readyz, /metrics) и проверяет окружение —
Valkey и видимость GPU в контейнере. Обработка видео ещё не реализована, поэтому список
capabilities пуст и core-api показывает сервис жёлтым (SPEC.md §2.10, docs/TODO.md).
"""

from __future__ import annotations

import os

from shared.telemetry.checks import gpu_check, valkey_check
from shared.telemetry.runner import run
from shared.telemetry.service import ReadinessCheck
from vision_pipeline import __version__


def main() -> None:
    checks: dict[str, ReadinessCheck] = {"valkey": valkey_check()}
    if os.environ.get("QOIMA_REQUIRE_GPU") == "1":
        checks["gpu"] = gpu_check()
    run(
        service="vision-pipeline",
        version=__version__,
        checks=checks,
        capabilities=[],
        notes=[
            "Camera Health — Фаза 1",
            "декодирование, инференс, трекинг и FSM событий — Фаза 2",
        ],
    )


if __name__ == "__main__":
    main()
