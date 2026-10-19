"""Запуск фонового сервиса: логи, метрики, /healthz и /readyz, корректная остановка по SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable

from prometheus_client import CollectorRegistry, PlatformCollector, ProcessCollector

from shared.telemetry.logging import configure_logging, get_logger
from shared.telemetry.metrics import build_info
from shared.telemetry.service import ReadinessCheck, ServiceRuntime

WorkFn = Callable[[asyncio.Event], Awaitable[None]]


async def serve(
    *,
    service: str,
    version: str,
    checks: dict[str, ReadinessCheck],
    capabilities: list[str],
    notes: list[str],
    work: WorkFn | None = None,
    registry: CollectorRegistry | None = None,
) -> None:
    """Поднимает служебный HTTP и ждёт SIGTERM/SIGINT. work — основной цикл сервиса (если есть)."""
    log = get_logger(service)
    reg = registry or CollectorRegistry()
    ProcessCollector(registry=reg)
    PlatformCollector(registry=reg)
    build_info(service, version, reg)

    runtime = ServiceRuntime(
        service=service,
        version=version,
        registry=reg,
        checks=checks,
        capabilities=capabilities,
        notes=notes,
        check_timeout_s=float(os.environ.get("QOIMA_READINESS_TIMEOUT_S", "2")),
    )
    port = await runtime.start(
        os.environ.get("QOIMA_SERVICE_HOST", "0.0.0.0"),  # noqa: S104 — слушаем внутри контейнера
        int(os.environ.get("QOIMA_SERVICE_PORT", "8080")),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("service_started", port=port, capabilities=capabilities)
    try:
        if work is not None:
            await work(stop)
        await stop.wait()
    finally:
        await runtime.stop()
        log.info("service_stopped")


def run(
    *,
    service: str,
    version: str,
    checks: dict[str, ReadinessCheck],
    capabilities: list[str],
    notes: list[str],
    work: WorkFn | None = None,
) -> None:
    configure_logging(service, os.environ.get("QOIMA_LOG_LEVEL", "INFO"))
    asyncio.run(
        serve(
            service=service,
            version=version,
            checks=checks,
            capabilities=capabilities,
            notes=notes,
            work=work,
        )
    )
