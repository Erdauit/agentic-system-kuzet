"""Точка входа worker-gpu.

Фаза 0: служебный HTTP, проверка Valkey и видимости GPU. Потребители задач появятся в Фазах 6–7
(docs/TODO.md), до этого capabilities пуст.
"""

from __future__ import annotations

import os

from shared.telemetry.checks import gpu_check, valkey_check
from shared.telemetry.runner import run
from shared.telemetry.service import ReadinessCheck
from workers import __version__


def main() -> None:
    checks: dict[str, ReadinessCheck] = {"valkey": valkey_check()}
    if os.environ.get("QOIMA_REQUIRE_GPU") == "1":
        checks["gpu"] = gpu_check()
    run(
        service="worker-gpu",
        version=__version__,
        checks=checks,
        capabilities=[],
        notes=["эмбеддинги и авторазметка — Фаза 6", "обучение и экспорт TensorRT — Фаза 7"],
    )


if __name__ == "__main__":
    main()
