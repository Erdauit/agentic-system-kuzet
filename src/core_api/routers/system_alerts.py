"""Системные алерты — только site_admin и qoima_engineer (SPEC.md §6.1)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from core_api.auth.deps import AdminUser
from core_api.context import AppContext, get_ctx
from shared.schemas import SystemAlert

router = APIRouter(prefix="/api/v1/system-alerts", tags=["system-alerts"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


@router.get("", response_model=list[SystemAlert])
async def list_alerts(_user: AdminUser, ctx: Ctx, active: bool = True) -> list[SystemAlert]:
    return await (ctx.alerts.active_alerts() if active else ctx.alerts.recent_alerts())


@router.post("/{alert_id}/ack", response_model=SystemAlert)
async def ack(alert_id: uuid.UUID, user: AdminUser, ctx: Ctx) -> SystemAlert:
    alert = await ctx.alerts.acknowledge(alert_id, user.actor)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "алерт не найден")
    return alert
