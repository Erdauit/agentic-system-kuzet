"""Маршруты здоровья: liveness для Docker, сводка «Состояние системы», SLO-панель."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from core_api import __version__
from core_api.auth.deps import CurrentUser
from core_api.context import AppContext, get_ctx
from core_api.metrics_query import SLO_OFFLINE_P95, QueryError
from core_api.system_health import HealthDeps, collect
from shared.schemas import SystemHealth

router = APIRouter(prefix="/api/v1", tags=["health"])


def get_deps(request: Request) -> HealthDeps:
    deps: HealthDeps = request.app.state.health_deps
    return deps


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: процесс жив и отвечает. Зависимости не проверяются, вход не нужен."""
    return {"service": "core-api", "status": "alive", "version": __version__}


@router.get("/health", response_model=SystemHealth)
async def system_health(
    _user: CurrentUser, deps: Annotated[HealthDeps, Depends(get_deps)]
) -> SystemHealth:
    return await collect(deps)


@router.get("/slo")
async def slo(
    _user: CurrentUser, ctx: Annotated[AppContext, Depends(get_ctx)]
) -> list[dict[str, Any]]:
    """SLO-панель (SPEC.md §6.1). Измеряется то, что уже работает; остальное — честно «не измеряется»."""
    targets = ctx.config.defaults.slo
    offline: float | None = None
    error: str | None = None
    if ctx.prometheus is not None:
        try:
            offline = await ctx.prometheus.scalar(SLO_OFFLINE_P95)
        except QueryError as exc:
            error = str(exc)
    rows: list[dict[str, Any]] = [
        {
            "name": "camera_offline_detection",
            "title": "Обнаружение офлайн-камеры (p95, 7 дней)",
            "target_s": targets.camera_offline_detection_s,
            "value_s": offline,
            "status": "not_measured"
            if offline is None
            else ("ok" if offline <= targets.camera_offline_detection_s else "violated"),
            "note": error or ("пока не было отключений камер" if offline is None else None),
        }
    ]
    for name, title, target in (
        ("fall_to_alert", "Падение → алерт (p95)", targets.fall_to_alert_p95_s),
        ("red_zone_to_alert", "Красная зона → алерт (p95)", targets.red_zone_to_alert_p95_s),
        ("smoke_fire_candidate", "Дым/огонь → алерт (p95)", targets.smoke_fire_candidate_p95_s),
    ):
        rows.append(
            {
                "name": name,
                "title": title,
                "target_s": target,
                "value_s": None,
                "status": "not_measured",
                "note": "детекторы и алерты подключаются в Фазе 2",
            }
        )
    return rows
