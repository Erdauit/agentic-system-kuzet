"""Инциденты и события: лента, карточка, подтверждение, закрытие с отзывом; медиа (клипы и кадры).

Доступ — любая роль после входа (операторы смены работают с инцидентами). Каждое действие
пишется в аудит. Комментарий оператора хранится как данные и никуда не исполняется (§2.6).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import Field

from core_api.api_models import ApiModel
from core_api.auth.deps import CurrentUser
from core_api.context import AppContext, get_ctx
from core_api.incidents.manager import IncidentError, IncidentView
from shared.schemas import Event, FeedbackVerdict, Incident, IncidentState, ReasonCode
from shared.storage.blobstore import InvalidBlobKeyError, validate_key

router = APIRouter(prefix="/api/v1", tags=["incidents"])
Ctx = Annotated[AppContext, Depends(get_ctx)]

MEDIA_PREFIXES = ("clips/", "frames/")
MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class DeliveryOut(ApiModel):
    channel: str
    target: str
    level: int
    status: str
    attempt: int
    error: str | None
    sent_at: str | None


class IncidentOut(ApiModel):
    incident: Incident
    events: list[Event]
    deliveries: list[DeliveryOut] = Field(default_factory=list)


class ResolveIn(ApiModel):
    verdict: FeedbackVerdict
    reason_code: ReasonCode | None = None
    comment: str | None = Field(default=None, max_length=1000)


async def _out(ctx: AppContext, view: IncidentView, with_deliveries: bool) -> IncidentOut:
    deliveries = []
    if with_deliveries:
        deliveries = [
            DeliveryOut(
                channel=d.channel,
                target=d.target,
                level=d.escalation_level,
                status=d.status,
                attempt=d.attempt,
                error=d.last_error,
                sent_at=d.sent_at.isoformat() if d.sent_at else None,
            )
            for d in await ctx.delivery.deliveries(view.incident.id)
        ]
    return IncidentOut(incident=view.incident, events=view.events, deliveries=deliveries)


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(
    _user: CurrentUser,
    ctx: Ctx,
    state: Annotated[list[IncidentState] | None, Query()] = None,
    scope: Literal["active", "all"] = "all",
    synthetic: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[IncidentOut]:
    states = state
    if scope == "active":
        states = [IncidentState.OPEN, IncidentState.ACKNOWLEDGED]
    views = await ctx.incidents.list_incidents(
        states=states, include_synthetic=synthetic, limit=limit
    )
    return [await _out(ctx, v, False) for v in views]


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: uuid.UUID, _user: CurrentUser, ctx: Ctx) -> IncidentOut:
    view = await ctx.incidents.get(incident_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "инцидент не найден")
    return await _out(ctx, view, True)


@router.post("/incidents/{incident_id}/ack", response_model=Incident)
async def acknowledge(incident_id: uuid.UUID, user: CurrentUser, ctx: Ctx) -> Incident:
    try:
        return await ctx.incidents.acknowledge(incident_id, user.actor)
    except IncidentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/incidents/{incident_id}/resolve", response_model=Incident)
async def resolve(incident_id: uuid.UUID, body: ResolveIn, user: CurrentUser, ctx: Ctx) -> Incident:
    try:
        return await ctx.incidents.resolve(
            incident_id, user.actor, body.verdict, body.reason_code, body.comment
        )
    except IncidentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/events", response_model=list[Event])
async def recent_events(
    _user: CurrentUser, ctx: Ctx, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[Event]:
    return await ctx.incidents.recent_events(limit)


@router.get("/media/{key:path}")
async def media(key: str, _user: CurrentUser, ctx: Ctx) -> FileResponse:
    """Клип или кадр из BlobStore. Только каталоги clips/ и frames/; Range поддерживается
    (перемотка клипа в браузере)."""
    try:
        validate_key(key)
    except InvalidBlobKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "некорректный ключ") from exc
    suffix = key[key.rfind(".") :].lower() if "." in key else ""
    if not key.startswith(MEDIA_PREFIXES) or suffix not in MEDIA_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такого файла")
    if not ctx.blobs.exists(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "файл удалён или ещё не готов")
    return FileResponse(
        ctx.blobs.root / key,
        media_type=MEDIA_TYPES[suffix],
        headers={"Cache-Control": "private, max-age=3600"},
    )
