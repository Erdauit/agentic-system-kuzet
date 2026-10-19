"""Сборка FastAPI-приложения core-api."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

from core_api import __version__
from core_api.db.migrate import head_revision
from core_api.routers import health
from core_api.settings import Settings
from core_api.system_health import HealthDeps
from shared.config import QoimaConfig, load_config
from shared.storage import postgres, valkey
from shared.telemetry import build_info, create_metric, get_logger


def _metrics_registry() -> CollectorRegistry:
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    build_info("core-api", __version__, registry)
    return registry


def _mount_ui(app: FastAPI, ui_dir: Path | None) -> None:
    """SPA раздаётся core-api как статика (SPEC.md §4). Любой не-API путь отдаёт index.html,
    чтобы работали прямые ссылки на разделы."""
    if ui_dir is None or not (ui_dir / "index.html").is_file():

        @app.get("/", include_in_schema=False)
        async def no_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "detail": "UI не собран: выполните `make ui-build` или используйте образ core-api"
                },
                status_code=503,
            )

        return

    root = ui_dir.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "metrics")):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app(
    settings: Settings | None = None,
    *,
    config: QoimaConfig | None = None,
    health_deps: HealthDeps | None = None,
) -> FastAPI:
    """health_deps передают тесты; в рабочем режиме зависимости создаются в lifespan."""
    settings = settings or Settings.from_env()
    registry = _metrics_registry()
    disk_gauge = create_metric("disk_free_bytes", registry)
    if not isinstance(disk_gauge, Gauge):
        raise TypeError("disk_free_bytes в каталоге метрик должен быть gauge")

    def watch_disk(cfg: QoimaConfig) -> None:
        # Значение считается при каждом скрейпе Prometheus, а не только при открытии UI.
        root = cfg.defaults.storage.data_root

        def free_bytes() -> float:
            try:
                return float(shutil.disk_usage(root).free)
            except OSError:
                return float("nan")

        disk_gauge.labels(volume="data").set_function(free_bytes)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if health_deps is not None:
            watch_disk(health_deps.config)
            app.state.health_deps = health_deps
            yield
            return
        cfg = config or load_config(settings.config_dir)
        watch_disk(cfg)
        engine = postgres.create_engine()
        vk = valkey.create_client()
        timeout = cfg.defaults.health.component_timeout_s
        async with httpx.AsyncClient(timeout=timeout) as http:
            app.state.health_deps = HealthDeps(
                config=cfg,
                settings=settings,
                engine=engine,
                valkey=vk,
                http=http,
                alembic_head=head_revision(),
            )
            get_logger("core-api").info(
                "core_api_started", version=__version__, site=cfg.defaults.site.name
            )
            try:
                yield
            finally:
                await vk.aclose()
                await engine.dispose()

    app = FastAPI(
        title="Qoima Vision core-api",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.include_router(health.router)

    # Явный маршрут, а не mount: Prometheus запрашивает ровно /metrics (без завершающего «/»).
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    _mount_ui(app, settings.ui_dir)
    return app
