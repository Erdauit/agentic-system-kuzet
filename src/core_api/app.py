"""Сборка FastAPI-приложения core-api."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    start_http_server,
)

from core_api import __version__
from core_api.alerts.channels import ChannelDispatcher
from core_api.alerts.delivery import DeliveryService
from core_api.alerts.system import SystemAlertManager
from core_api.auth.service import AuthService
from core_api.cameras.credentials import CredentialStore
from core_api.cameras.jobs import DiscoveryJobs
from core_api.cameras.mediamtx import MediaMtxClient
from core_api.cameras.service import CameraService
from core_api.capacity.service import CapacityService
from core_api.context import AppContext
from core_api.db.migrate import head_revision
from core_api.incidents.manager import IncidentManager
from core_api.metrics_query import PrometheusClient
from core_api.realtime import Broadcaster
from core_api.routers import (
    auth,
    cameras,
    capacity,
    health,
    incidents,
    realtime,
    setup,
    system_alerts,
    users,
    zones,
)
from core_api.selftest import SelfTestService
from core_api.settings import Settings
from core_api.site_config import SiteConfigStore
from core_api.system_health import HealthDeps, collect
from shared.config import QoimaConfig, load_config, resolve_config_dir
from shared.storage import LocalFSBlobStore, postgres, valkey
from shared.storage.secrets import SecretNotFoundError, read_secret
from shared.telemetry import build_info, create_metric, get_logger

log = get_logger("core-api")

# Периодическая синхронизация MediaMTX: пути, добавленные через API, теряются при его рестарте.
MEDIAMTX_RESYNC_S = 60
SESSION_PURGE_S = 3600


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
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)


async def _periodic(name: str, interval_s: float, fn: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # фоновые задачи не должны падать насовсем
            log.warning("background_task_failed", task=name, error=str(exc))
        await asyncio.sleep(interval_s)


def create_app(
    settings: Settings | None = None,
    *,
    config: QoimaConfig | None = None,
    health_deps: HealthDeps | None = None,
    context_factory: Callable[[FastAPI], Awaitable[AppContext]] | None = None,
) -> FastAPI:
    """health_deps/context_factory передают тесты; в рабочем режиме всё создаётся в lifespan."""
    settings = settings or Settings.from_env()
    registry = _metrics_registry()
    disk_gauge = create_metric("disk_free_bytes", registry)
    if not isinstance(disk_gauge, Gauge):
        raise TypeError("disk_free_bytes в каталоге метрик должен быть gauge")
    offline_detection = create_metric("camera_offline_detection_seconds", registry)
    if not isinstance(offline_detection, Histogram):
        raise TypeError("camera_offline_detection_seconds в каталоге должен быть histogram")

    def histogram(name: str) -> Histogram:
        m = create_metric(name, registry)
        if not isinstance(m, Histogram):
            raise TypeError(f"{name} в каталоге должен быть histogram")
        return m

    def counter(name: str) -> Counter:
        m = create_metric(name, registry)
        if not isinstance(m, Counter):
            raise TypeError(f"{name} в каталоге должен быть counter")
        return m

    alert_latency = histogram("alert_latency_seconds")
    ack_seconds = histogram("alert_ack_seconds")
    canary_seconds = histogram("canary_e2e_seconds")
    alerts_total = counter("alerts_total")
    escalations = counter("alert_escalations_total")
    canary_failures = counter("canary_failures_total")

    def watch_disk(root: Path) -> None:
        # Значение считается при каждом скрейпе Prometheus, а не только при открытии UI.
        def free_bytes() -> float:
            try:
                return float(shutil.disk_usage(root).free)
            except OSError:
                return float("nan")

        disk_gauge.labels(volume="data").set_function(free_bytes)

    async def build_context(app: FastAPI, cfg: QoimaConfig, http: httpx.AsyncClient) -> AppContext:
        engine = postgres.create_engine()
        vk = valkey.create_client()
        data_root = settings.data_root or cfg.defaults.storage.data_root
        broadcaster = Broadcaster()
        dispatcher = ChannelDispatcher(http)
        auth_service = AuthService(engine, cfg.defaults.auth)
        blobs = LocalFSBlobStore(data_root)
        holder: dict[str, AppContext] = {}

        def current_config() -> QoimaConfig:
            # До создания контекста (конструкторы сервисов) — стартовая конфигурация.
            return holder["ctx"].config if "ctx" in holder else cfg

        def names() -> dict[str, str]:
            return holder["ctx"].camera_names if "ctx" in holder else {}

        mediamtx = (
            MediaMtxClient(settings.mediamtx_api_url, http) if settings.mediamtx_api_url else None
        )
        camera_service = CameraService(
            engine,
            vk,
            blobs,
            CredentialStore(settings.runtime_secrets_dir),
            mediamtx,
            current_config,
            settings.mediamtx_rtsp_url,
        )
        alerts = SystemAlertManager(
            engine, vk, broadcaster, dispatcher, current_config, names, offline_detection
        )
        delivery = DeliveryService(
            engine,
            dispatcher,
            broadcaster,
            current_config,
            blobs,
            alerts,
            names,
            alert_latency,
            escalations,
        )
        dispatcher.web_hook = delivery.web_hook
        incident_manager = IncidentManager(
            engine, vk, broadcaster, delivery, current_config, alerts_total, ack_seconds
        )
        selftest = SelfTestService(
            engine, vk, incident_manager, alerts, current_config, canary_seconds, canary_failures
        )
        site_file = settings.site_config or (data_root / "config" / "site.yaml")
        ctx = AppContext(
            settings=settings,
            config=cfg,
            engine=engine,
            valkey=vk,
            http=http,
            blobs=blobs,
            auth=auth_service,
            cameras=camera_service,
            alerts=alerts,
            dispatcher=dispatcher,
            broadcaster=broadcaster,
            discovery=DiscoveryJobs(
                current_config, broadcaster, settings.host_subnets, ["mediamtx"]
            ),
            site=SiteConfigStore(resolve_config_dir(settings.config_dir), site_file),
            prometheus=PrometheusClient(settings.prometheus_url, http)
            if settings.prometheus_url
            else None,
            data_root=data_root,
            runtime_secrets=settings.runtime_secrets_dir,
            delivery=delivery,
            incidents=incident_manager,
            selftest=selftest,
            capacity=CapacityService(
                engine, current_config, data_root, camera_service, broadcaster
            ),
        )
        holder["ctx"] = ctx
        return ctx

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if health_deps is not None:
            watch_disk(health_deps.config.defaults.storage.data_root)
            app.state.health_deps = health_deps
            if context_factory is not None:
                app.state.ctx = await context_factory(app)
                app.state.auth = app.state.ctx.auth
            yield
            return
        cfg = config or load_config(settings.config_dir, settings.site_config)
        timeout = cfg.defaults.health.component_timeout_s
        async with httpx.AsyncClient(timeout=timeout) as http:
            ctx = await build_context(app, cfg, http)
            app.state.ctx = ctx
            app.state.auth = ctx.auth
            watch_disk(ctx.data_root)
            deps = HealthDeps(
                config=cfg,
                settings=settings,
                engine=ctx.engine,
                valkey=ctx.valkey,
                http=http,
                alembic_head=head_revision(),
                alerts=ctx.alerts,
                data_root=ctx.data_root,
            )
            app.state.health_deps = deps

            try:
                admin_password: str | None = read_secret(
                    "admin_initial_password", path_env="QOIMA_ADMIN_PASSWORD_FILE"
                ).get_secret_value()
            except SecretNotFoundError:
                admin_password = None
            await ctx.auth.bootstrap_admin(admin_password)
            await ctx.refresh_camera_names()

            async def health_tick() -> None:
                deps.config = ctx.config
                deps.telegram_token_present = (
                    ctx.runtime_secrets / ctx.config.alerts.channels.telegram.bot_token_secret
                ).is_file()
                await ctx.alerts.handle_system_health(await collect(deps))

            tasks: list[asyncio.Task[None]] = []
            stop = asyncio.Event()
            if settings.background_tasks:
                tasks = [
                    asyncio.create_task(ctx.incidents.run(stop)),
                    asyncio.create_task(ctx.delivery.run(stop)),
                    asyncio.create_task(ctx.selftest.canary_loop(stop)),
                    asyncio.create_task(ctx.alerts.consume()),
                    asyncio.create_task(ctx.alerts.watch_silence()),
                    asyncio.create_task(
                        _periodic("mediamtx_sync", MEDIAMTX_RESYNC_S, ctx.cameras.sync)
                    ),
                    asyncio.create_task(
                        _periodic(
                            "health", cfg.defaults.system_alerts.health_poll_interval_s, health_tick
                        )
                    ),
                    asyncio.create_task(
                        _periodic("session_purge", SESSION_PURGE_S, ctx.auth.purge_expired)
                    ),
                ]
            log.info("core_api_started", version=__version__, site=cfg.defaults.site.name)
            try:
                yield
            finally:
                stop.set()
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                await ctx.valkey.aclose()
                await ctx.engine.dispose()

    app = FastAPI(
        title="Qoima Vision core-api",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    for r in (
        health.router,
        auth.router,
        users.router,
        cameras.router,
        setup.router,
        system_alerts.router,
        realtime.router,
        incidents.router,
        zones.router,
        capacity.router,
    ):
        app.include_router(r)
    app.state.metrics_registry = registry
    if settings.metrics_port is not None:
        # /metrics — на отдельном внутреннем порту: на публичном порту UI метрики не видны.
        start_http_server(settings.metrics_port, registry=registry)
    _mount_ui(app, settings.ui_dir)
    return app
