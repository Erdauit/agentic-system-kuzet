"""Маршруты здоровья: liveness для Docker и сводка для раздела «Состояние системы»."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from core_api import __version__
from core_api.system_health import HealthDeps, collect
from shared.schemas import SystemHealth

router = APIRouter(prefix="/api/v1", tags=["health"])


def get_deps(request: Request) -> HealthDeps:
    deps: HealthDeps = request.app.state.health_deps
    return deps


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: процесс жив и отвечает. Зависимости не проверяются."""
    return {"service": "core-api", "status": "alive", "version": __version__}


@router.get("/health", response_model=SystemHealth)
async def system_health(deps: Annotated[HealthDeps, Depends(get_deps)]) -> SystemHealth:
    return await collect(deps)
