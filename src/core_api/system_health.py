"""Сбор состояния компонентов для раздела «Состояние системы» (SPEC.md §6.1).

Каждая проверка возвращает статус и понятное человеку сообщение. Проверки выполняются
параллельно, каждая — с таймаутом health.component_timeout_s: один зависший сервис не должен
подвешивать всю страницу. Компонент, который ещё не реализован или не развёрнут, показывается
честно (NOT_DEPLOYED / YELLOW), а не зелёным (SPEC.md §2.10).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from valkey.asyncio import Valkey

from core_api.db import models
from core_api.settings import Settings
from shared.config import QoimaConfig
from shared.schemas import ComponentHealth, ComponentKind, HealthStatus, ModelStatus, SystemHealth
from shared.schemas.health import DetailValue


@dataclass(frozen=True)
class Probe:
    status: HealthStatus
    message: str
    details: dict[str, DetailValue] | None = None


@dataclass
class HealthDeps:
    config: QoimaConfig
    settings: Settings
    engine: AsyncEngine | None
    valkey: Valkey | None
    http: httpx.AsyncClient
    alembic_head: str


CheckFn = Callable[[HealthDeps], Awaitable[Probe]]


@dataclass(frozen=True)
class ComponentCheck:
    name: str
    kind: ComponentKind
    fn: CheckFn


# ---------------------------------------------------------------- инфраструктура


async def check_database(deps: HealthDeps) -> Probe:
    if deps.engine is None:
        return Probe(HealthStatus.RED, "Подключение к Postgres не настроено")
    async with deps.engine.connect() as conn:
        revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    if revision != deps.alembic_head:
        return Probe(
            HealthStatus.RED,
            f"Схема БД не на актуальной версии: {revision or 'нет'} вместо {deps.alembic_head}. "
            "Перезапустите core-api или выполните миграции.",
            {"revision": revision, "expected": deps.alembic_head},
        )
    return Probe(HealthStatus.GREEN, "Postgres доступен, схема актуальна", {"revision": revision})


async def check_queues(deps: HealthDeps) -> Probe:
    if deps.valkey is None:
        return Probe(HealthStatus.RED, "Подключение к Valkey не настроено")
    if not await deps.valkey.ping():
        return Probe(HealthStatus.RED, "Valkey не ответил на PING")
    return Probe(HealthStatus.GREEN, "Valkey доступен")


async def check_vector_db(deps: HealthDeps) -> Probe:
    if not deps.settings.qdrant_url:
        return Probe(HealthStatus.NOT_DEPLOYED, "Qdrant не настроен в этой конфигурации")
    resp = await deps.http.get(f"{deps.settings.qdrant_url}/readyz")
    if resp.status_code != 200:
        return Probe(HealthStatus.RED, f"Qdrant не готов (HTTP {resp.status_code})")
    return Probe(HealthStatus.GREEN, "Qdrant доступен")


async def check_media_server(deps: HealthDeps) -> Probe:
    if not deps.settings.mediamtx_api_url:
        return Probe(HealthStatus.NOT_DEPLOYED, "MediaMTX не настроен в этой конфигурации")
    resp = await deps.http.get(f"{deps.settings.mediamtx_api_url}/v3/paths/list")
    if resp.status_code != 200:
        return Probe(HealthStatus.RED, f"MediaMTX API отвечает HTTP {resp.status_code}")
    paths = resp.json().get("itemCount", 0)
    return Probe(HealthStatus.GREEN, f"MediaMTX доступен, потоков: {paths}", {"paths": paths})


async def check_triton(deps: HealthDeps) -> Probe:
    if not deps.settings.triton_url:
        return Probe(
            HealthStatus.NOT_DEPLOYED, "Triton не запущен: система поднята без GPU-профиля"
        )
    resp = await deps.http.get(f"{deps.settings.triton_url}/v2/health/ready")
    if resp.status_code != 200:
        return Probe(HealthStatus.RED, f"Triton не готов (HTTP {resp.status_code})")
    return Probe(HealthStatus.GREEN, "Triton готов")


async def check_llm(_deps: HealthDeps) -> Probe:
    # Сервис llm появится в Фазе 4 после бенчмарка vLLM vs SGLang (SPEC.md §4, docs/TODO.md).
    return Probe(
        HealthStatus.NOT_DEPLOYED,
        "LLM-сервис ещё не развёрнут (Фаза 4). Алерты уходят без VLM-арбитража — это штатный fail-open.",
    )


async def check_alert_channels(_deps: HealthDeps) -> Probe:
    return Probe(HealthStatus.NOT_DEPLOYED, "Каналы алертов реализуются в Фазе 2")


async def check_time(_deps: HealthDeps) -> Probe:
    # Контейнер не видит состояние chrony/NTP хоста. До Фазы 1 время проверяет `qoima doctor`.
    return Probe(
        HealthStatus.UNKNOWN,
        "Синхронизация времени проверяется командой `qoima doctor` на сервере; в UI — с Фазы 1",
    )


async def check_disk(deps: HealthDeps) -> Probe:
    root: Path = deps.config.defaults.storage.data_root
    if not await asyncio.to_thread(root.exists):
        return Probe(HealthStatus.RED, f"Каталог данных {root} не существует")
    usage = await asyncio.to_thread(shutil.disk_usage, root)
    ratio = usage.free / usage.total if usage.total else 0.0
    cfg = deps.config.defaults.health
    details: dict[str, DetailValue] = {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free_ratio": round(ratio, 4),
    }
    free_gb = usage.free / 1024**3
    if ratio < cfg.disk_critical_free_ratio:
        return Probe(
            HealthStatus.RED,
            f"Свободно {free_gb:.0f} ГБ ({ratio:.0%}) — клипы могут не записаться",
            details,
        )
    if ratio < cfg.disk_warn_free_ratio:
        return Probe(
            HealthStatus.YELLOW,
            f"Свободно {free_gb:.0f} ГБ ({ratio:.0%}) — пора освободить место",
            details,
        )
    return Probe(HealthStatus.GREEN, f"Свободно {free_gb:.0f} ГБ ({ratio:.0%})", details)


# ---------------------------------------------------------------- данные из БД


async def check_cameras(deps: HealthDeps) -> Probe:
    if deps.engine is None:
        return Probe(HealthStatus.UNKNOWN, "Нет данных: БД недоступна")
    async with deps.engine.connect() as conn:
        count = (
            await conn.execute(
                select(func.count()).select_from(models.Camera).where(models.Camera.enabled)
            )
        ).scalar_one()
    if count == 0:
        return Probe(
            HealthStatus.YELLOW,
            "Камеры не добавлены. Подключение камер — мастер настройки (Фаза 1)",
        )
    return Probe(
        HealthStatus.UNKNOWN,
        f"Камер: {count}. Camera Health (проверка потоков) реализуется в Фазе 1",
        {"cameras": count},
    )


async def check_models(deps: HealthDeps) -> Probe:
    if deps.engine is None:
        return Probe(HealthStatus.UNKNOWN, "Нет данных: БД недоступна")
    async with deps.engine.connect() as conn:
        active = (
            await conn.execute(
                select(func.count())
                .select_from(models.Model)
                .where(models.Model.status == ModelStatus.ACTIVE.value)
            )
        ).scalar_one()
    if active == 0:
        return Probe(
            HealthStatus.YELLOW, "Нет активных моделей: базовые детекторы подключаются в Фазе 2"
        )
    return Probe(HealthStatus.GREEN, f"Активных моделей: {active}", {"active": active})


# ---------------------------------------------------------------- фоновые сервисы Qoima


def service_check(service: str) -> CheckFn:
    """Проверка сервиса по его /readyz (shared.telemetry.service.ServiceRuntime)."""

    async def check(deps: HealthDeps) -> Probe:
        url = deps.settings.service_urls.get(service)
        if not url:
            return Probe(
                HealthStatus.NOT_DEPLOYED, f"Сервис {service} не развёрнут в этой конфигурации"
            )
        try:
            resp = await deps.http.get(f"{url}/readyz")
        except httpx.HTTPError as exc:
            return Probe(HealthStatus.RED, f"Сервис {service} недоступен: {type(exc).__name__}")
        body = resp.json()
        if resp.status_code != 200:
            failed = [name for name, c in body.get("checks", {}).items() if not c.get("ok")]
            return Probe(
                HealthStatus.RED, f"Сервис {service} не готов: {', '.join(failed) or 'нет данных'}"
            )
        capabilities = body.get("capabilities") or []
        details: dict[str, DetailValue] = {
            "version": body.get("version"),
            "capabilities": ", ".join(capabilities),
        }
        if not capabilities:
            notes = "; ".join(body.get("notes") or []) or "функциональность ещё не реализована"
            return Probe(
                HealthStatus.YELLOW, f"Процесс запущен, но не выполняет работу: {notes}", details
            )
        return Probe(HealthStatus.GREEN, f"Работает: {', '.join(capabilities)}", details)

    return check


CHECKS: tuple[ComponentCheck, ...] = (
    ComponentCheck("cameras", ComponentKind.CAMERAS, check_cameras),
    ComponentCheck("vision-pipeline", ComponentKind.PIPELINE, service_check("vision-pipeline")),
    ComponentCheck("models", ComponentKind.MODELS, check_models),
    ComponentCheck("triton", ComponentKind.TRITON, check_triton),
    ComponentCheck("llm", ComponentKind.LLM, check_llm),
    ComponentCheck("valkey", ComponentKind.QUEUES, check_queues),
    ComponentCheck("postgres", ComponentKind.DATABASE, check_database),
    ComponentCheck("qdrant", ComponentKind.VECTOR_DB, check_vector_db),
    ComponentCheck("mediamtx", ComponentKind.MEDIA_SERVER, check_media_server),
    ComponentCheck("agents", ComponentKind.AGENTS, service_check("agents")),
    ComponentCheck("worker-gpu", ComponentKind.WORKER, service_check("worker-gpu")),
    ComponentCheck("disk", ComponentKind.DISK, check_disk),
    ComponentCheck("time", ComponentKind.TIME, check_time),
    ComponentCheck("alert-channels", ComponentKind.ALERT_CHANNELS, check_alert_channels),
)


async def run_component(
    check: ComponentCheck, deps: HealthDeps, timeout_s: float
) -> ComponentHealth:
    started = time.perf_counter()
    try:
        probe = await asyncio.wait_for(check.fn(deps), timeout=timeout_s)
    except TimeoutError:
        probe = Probe(HealthStatus.RED, f"Проверка не уложилась в {timeout_s} с")
    except Exception as exc:  # любая ошибка проверки — это красный статус, а не 500 на странице
        probe = Probe(HealthStatus.RED, f"Ошибка проверки: {type(exc).__name__}: {exc}"[:500])
    return ComponentHealth(
        component=check.name,
        kind=check.kind,
        status=probe.status,
        message=probe.message,
        checked_at=datetime.now(UTC),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        details=probe.details or {},
    )


async def collect(deps: HealthDeps, checks: tuple[ComponentCheck, ...] = CHECKS) -> SystemHealth:
    timeout_s = deps.config.defaults.health.component_timeout_s
    components = await asyncio.gather(*(run_component(c, deps, timeout_s) for c in checks))
    return SystemHealth.from_components(list(components), generated_at=datetime.now(UTC))
