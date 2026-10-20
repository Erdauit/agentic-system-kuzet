"""Зоны, участки и калибровка пола камеры (мастер, шаг 5).

Зоны рисуются на снапшоте камеры в нормализованных координатах. Участок (area) объединяет
зоны разных камер, которые видят одно место: события одного типа на участке — один инцидент.
Калибровка пола — 4 точки на полу и (необязательно) известное расстояние между двумя из них;
гомография считается здесь и публикуется в реестр камер для vision-pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from core_api.api_models import ApiModel
from core_api.auth.deps import AdminUser, CurrentUser
from core_api.cameras.service import CameraError, baseline_key, zone_from_row
from core_api.context import AppContext, get_ctx
from shared.media.geometry import CalibrationError, floor_homography
from shared.schemas import FloorCalibration, KnownDistance, Point, Zone

router = APIRouter(prefix="/api/v1/cameras", tags=["zones"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


class ZonesIn(ApiModel):
    zones: list[Zone] = Field(max_length=50)


class CalibrationIn(ApiModel):
    image_points: list[Point] = Field(min_length=4, max_length=4)
    known_distance: KnownDistance | None = None


class CalibrationOut(ApiModel):
    calibration: FloorCalibration | None
    metric: bool


@router.get("/{camera_id}/zones", response_model=list[Zone])
async def get_zones(camera_id: str, _user: CurrentUser, ctx: Ctx) -> list[Zone]:
    if await ctx.cameras.get(camera_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "камера не найдена")
    return [zone_from_row(z) for z in await ctx.cameras.list_zones(camera_id)]


@router.put("/{camera_id}/zones", response_model=list[Zone])
async def put_zones(camera_id: str, body: ZonesIn, user: AdminUser, ctx: Ctx) -> list[Zone]:
    try:
        return await ctx.cameras.replace_zones(camera_id, body.zones, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/{camera_id}/calibration", response_model=CalibrationOut)
async def get_calibration(camera_id: str, _user: CurrentUser, ctx: Ctx) -> CalibrationOut:
    row = await ctx.cameras.get(camera_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "камера не найдена")
    cal = FloorCalibration.model_validate(row.calibration) if row.calibration else None
    return CalibrationOut(calibration=cal, metric=bool(cal and cal.known_distance))


@router.put("/{camera_id}/calibration", response_model=CalibrationOut)
async def put_calibration(
    camera_id: str, body: CalibrationIn, user: AdminUser, ctx: Ctx
) -> CalibrationOut:
    if await ctx.cameras.get(camera_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "камера не найдена")
    pts = [(p.x, p.y) for p in body.image_points]
    known = body.known_distance
    try:
        h, metric = floor_homography(
            pts, (known.point_a, known.point_b, known.meters) if known else None
        )
    except CalibrationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"точки не задают плоскость пола: {exc}. Отметьте 4 угла на полу, не на одной линии.",
        ) from exc
    cal = FloorCalibration(
        camera_id=camera_id,
        image_points=body.image_points,
        known_distance=known,
        homography=[[float(v) for v in row] for row in h],
        snapshot_key=baseline_key(camera_id),
        calibrated_at=datetime.now(UTC),
    )
    try:
        await ctx.cameras.set_calibration(camera_id, cal, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return CalibrationOut(calibration=cal, metric=metric)


@router.delete("/{camera_id}/calibration", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calibration(camera_id: str, user: AdminUser, ctx: Ctx) -> None:
    try:
        await ctx.cameras.set_calibration(camera_id, None, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
