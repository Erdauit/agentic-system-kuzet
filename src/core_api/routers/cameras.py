"""Камеры: список и состояние, автопоиск и подключение найденного, ручное добавление, импорт,
базовая линия, снапшот, история метрик, live-просмотр (WebRTC/WHEP через core-api)."""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from typing import Annotated, Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from core_api.alerts.system import effective_status
from core_api.api_models import (
    AddDiscoveredIn,
    AddResult,
    CameraCreatedOut,
    CameraCreateIn,
    CameraOut,
    CameraPatchIn,
    CredentialsIn,
    DiscoveryIn,
    HistoryOut,
    ImportIn,
    ProbeIn,
)
from core_api.audit import write_audit
from core_api.auth.deps import AdminUser, CurrentUser
from core_api.cameras.mediamtx import main_path, sub_path
from core_api.cameras.onvif import Credentials
from core_api.cameras.service import (
    CameraError,
    CreatedCamera,
    NewCamera,
    baseline_key,
    config_from_row,
)
from core_api.context import AppContext, get_ctx
from core_api.db import models
from core_api.metrics_query import QueryError
from shared.schemas import (
    CameraSource,
    DiscoveryJob,
    ProbeReport,
    QualityBaseline,
    StreamProbe,
    VideoCodec,
)

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


def _creds(c: CredentialsIn | None) -> Credentials | None:
    return Credentials(c.username, c.password.get_secret_value()) if c else None


def camera_out(ctx: AppContext, row: models.Camera) -> CameraOut:
    health = ctx.alerts.camera_status(row.id) if row.enabled else None
    stale = health is not None and ctx.alerts.is_stale(health)
    probes = [StreamProbe.model_validate(p) for p in row.probes or []]
    analysis = next((p for p in probes if p.role.value == "sub"), probes[0] if probes else None)
    return CameraOut(
        camera=config_from_row(row),
        status=effective_status(health, stale),
        health=health,
        baseline=QualityBaseline.model_validate(row.baseline) if row.baseline else None,
        probes=probes,
        has_credentials=ctx.cameras.credentials.has(row.id),
        live_available=analysis is not None and analysis.codec is VideoCodec.H264,
    )


async def _row(ctx: AppContext, camera_id: str) -> models.Camera:
    row = await ctx.cameras.get(camera_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "камера не найдена")
    return row


async def _after_change(ctx: AppContext) -> None:
    await ctx.refresh_camera_names()


@router.get("", response_model=list[CameraOut])
async def list_cameras(_user: CurrentUser, ctx: Ctx) -> list[CameraOut]:
    return [camera_out(ctx, r) for r in await ctx.cameras.list_all()]


# ---------------------------------------------------------------- автопоиск


@router.post("/discover", response_model=DiscoveryJob, status_code=status.HTTP_202_ACCEPTED)
async def start_discovery(body: DiscoveryIn, user: AdminUser, ctx: Ctx) -> DiscoveryJob:
    creds = [Credentials(c.username, c.password.get_secret_value()) for c in body.credentials]
    job = ctx.discovery.start(body.subnets, creds)
    async with ctx.cameras.sessions() as s:
        await write_audit(
            s,
            actor=user.actor,
            action="discovery",
            details={"subnets": job.subnets, "hosts": job.hosts_total},
        )
        await s.commit()
    return job


@router.get("/discover/{job_id}", response_model=DiscoveryJob)
async def discovery_status(job_id: uuid.UUID, _user: AdminUser, ctx: Ctx) -> DiscoveryJob:
    state = ctx.discovery.get(job_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "задача поиска не найдена (или устарела)")
    return state.job


@router.post("/discover/{job_id}/add", response_model=list[AddResult])
async def add_discovered(
    job_id: uuid.UUID, body: AddDiscoveredIn, user: AdminUser, ctx: Ctx
) -> list[AddResult]:
    """Подключить найденные устройства (все каналы каждого) одной командой."""
    state = ctx.discovery.get(job_id)
    if state is None or state.job.status != "done":
        raise HTTPException(status.HTTP_409_CONFLICT, "поиск не завершён или устарел")
    devices = {d.host: d for d in state.job.devices}
    existing = await ctx.cameras.existing_hosts()

    async def one(host: str) -> list[AddResult]:
        device = devices.get(host)
        if device is None:
            return [
                AddResult(host=host, ok=False, error="устройство не найдено в результатах поиска")
            ]
        if host in existing:
            return [AddResult(host=host, ok=False, error="камера с этим адресом уже подключена")]
        if not device.candidates:
            return [
                AddResult(
                    host=host, ok=False, error="нет адресов потоков — проверьте логин и пароль"
                )
            ]
        results = await ctx.cameras.add_discovered(device, state.credentials.get(host), user.actor)
        out = []
        for r in results:
            if isinstance(r, CreatedCamera):
                out.append(
                    AddResult(
                        host=host,
                        ok=True,
                        camera_id=r.camera.id,
                        name=r.camera.name,
                        warnings=r.report.warnings,
                    )
                )
            else:
                out.append(AddResult(host=host, ok=False, error=str(r)))
        return out

    nested = await asyncio.gather(*(one(h) for h in body.hosts))
    await _after_change(ctx)
    return [r for group in nested for r in group]


@router.post("/probe", response_model=ProbeReport)
async def probe(body: ProbeIn, _user: AdminUser, ctx: Ctx) -> ProbeReport:
    creds = _creds(body.credentials)
    if creds is None and body.discovery_job_id is not None:
        state = ctx.discovery.get(body.discovery_job_id)
        creds = state.credentials.get(body.host) if state else None
    report, _main, _sub = await ctx.cameras.probe(body.host, body.candidates, creds, body.channel)
    return report


# ---------------------------------------------------------------- ручное добавление и импорт


async def _create(
    ctx: AppContext, body: CameraCreateIn, source: CameraSource, actor: str
) -> CameraCreatedOut:
    try:
        created = await ctx.cameras.create(
            NewCamera(
                name=body.name,
                main_url=body.main_url,
                sub_url=body.sub_url,
                credentials=_creds(body.credentials),
                source=source,
                location=body.location,
                scenarios=[s.value for s in body.scenarios],
            ),
            actor,
        )
    except CameraError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError as exc:  # контракт отверг URL (например, пароль в адресе)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await _after_change(ctx)
    return CameraCreatedOut(camera=created.camera, report=created.report)


@router.post("", response_model=CameraCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_camera(body: CameraCreateIn, user: AdminUser, ctx: Ctx) -> CameraCreatedOut:
    return await _create(ctx, body, CameraSource.MANUAL, user.actor)


def parse_import(fmt: str, content: str) -> list[dict[str, Any]]:
    """Строки импорта: name, main_url, sub_url, username, password, location."""
    if fmt == "csv":
        sample = content[:4096]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.DictReader(io.StringIO(content), delimiter=delimiter))
    else:
        data = yaml.safe_load(content)
        if isinstance(data, dict) and "cameras" in data:
            data = data["cameras"]
        if not isinstance(data, list):
            raise ValueError("YAML: ожидается список камер (или ключ cameras со списком)")
        rows = [r for r in data if isinstance(r, dict)]
    out = []
    for i, r in enumerate(rows, start=1):
        row = {
            str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in r.items()
        }
        if not row.get("main_url") and row.get("url"):
            row["main_url"] = row["url"]
        if not row.get("main_url"):
            raise ValueError(f"строка {i}: нет main_url")
        out.append(row)
    if not out:
        raise ValueError("в файле нет камер")
    return out


@router.post("/import", response_model=list[AddResult])
async def import_cameras(body: ImportIn, user: AdminUser, ctx: Ctx) -> list[AddResult]:
    try:
        rows = parse_import(body.format, body.content)
    except (ValueError, yaml.YAMLError, csv.Error) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"не удалось разобрать файл: {exc}"
        ) from exc
    results: list[AddResult] = []
    for i, r in enumerate(rows, start=1):
        creds = None
        if r.get("username"):
            creds = CredentialsIn.model_validate(
                {"username": r["username"], "password": r.get("password", "")}
            )
        try:
            body_in = CameraCreateIn(
                name=r.get("name") or f"Камера {i}",
                main_url=r["main_url"],
                sub_url=r.get("sub_url") or None,
                credentials=creds,
                location=r.get("location") or None,
            )
            created = await _create(ctx, body_in, CameraSource.IMPORT, user.actor)
            results.append(
                AddResult(
                    host=r["main_url"],
                    ok=True,
                    camera_id=created.camera.id,
                    name=created.camera.name,
                    warnings=created.report.warnings,
                )
            )
        except HTTPException as exc:
            results.append(
                AddResult(host=r.get("name") or r["main_url"], ok=False, error=str(exc.detail))
            )
        except ValueError as exc:
            results.append(AddResult(host=r.get("name") or r["main_url"], ok=False, error=str(exc)))
    return results


# ---------------------------------------------------------------- одна камера


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: str, _user: CurrentUser, ctx: Ctx) -> CameraOut:
    return camera_out(ctx, await _row(ctx, camera_id))


@router.patch("/{camera_id}", response_model=CameraOut)
async def patch_camera(camera_id: str, body: CameraPatchIn, user: AdminUser, ctx: Ctx) -> CameraOut:
    changes = body.model_dump(exclude_none=True, mode="json")
    try:
        await ctx.cameras.update(camera_id, changes, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if changes.get("enabled") is False:
        await ctx.alerts.forget_camera(camera_id)
    await _after_change(ctx)
    return camera_out(ctx, await _row(ctx, camera_id))


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: str, user: AdminUser, ctx: Ctx) -> None:
    try:
        await ctx.cameras.delete(camera_id, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await ctx.alerts.forget_camera(camera_id)
    await _after_change(ctx)


@router.post("/{camera_id}/baseline", response_model=QualityBaseline)
async def recapture_baseline(camera_id: str, user: AdminUser, ctx: Ctx) -> QualityBaseline:
    try:
        return await ctx.cameras.recapture_baseline(camera_id, user.actor)
    except CameraError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/{camera_id}/snapshot")
async def snapshot(camera_id: str, _user: CurrentUser, ctx: Ctx) -> FileResponse:
    await _row(ctx, camera_id)
    key = baseline_key(camera_id)
    if not ctx.blobs.exists(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "снапшота нет")
    return FileResponse(
        ctx.blobs.root / key, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
    )


@router.get("/{camera_id}/history", response_model=HistoryOut)
async def history(
    camera_id: str, metric: str, range: str, _user: CurrentUser, ctx: Ctx
) -> HistoryOut:
    await _row(ctx, camera_id)
    if ctx.prometheus is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Prometheus не настроен")
    try:
        series = await ctx.prometheus.camera_history(camera_id, metric, range)
    except QueryError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return HistoryOut(camera_id=camera_id, metric=metric, range=range, points=series.points)


# ---------------------------------------------------------------- live (WHEP)
#
# Браузер говорит только с core-api (одна точка входа, та же авторизация). core-api проксирует
# сигнализацию WHEP (SDP offer/answer) в MediaMTX во внутренней сети; само видео идёт по WebRTC
# с опубликованного порта MediaMTX (8189 UDP/TCP).


def _whep_base(ctx: AppContext, row: models.Camera) -> str:
    if not ctx.settings.mediamtx_webrtc_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "live-просмотр не настроен")
    path = sub_path(row.id) if row.sub_url else main_path(row.id)
    return f"{ctx.settings.mediamtx_webrtc_url}/{path}/whep"


@router.post("/{camera_id}/whep")
async def whep_offer(camera_id: str, request: Request, _user: CurrentUser, ctx: Ctx) -> Response:
    row = await _row(ctx, camera_id)
    offer = await request.body()
    try:
        resp = await ctx.http.post(
            _whep_base(ctx, row), content=offer, headers={"Content-Type": "application/sdp"}
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"MediaMTX недоступен: {type(exc).__name__}"
        ) from exc
    headers: dict[str, str] = {}
    location = resp.headers.get("location")
    if location:
        session = location.rstrip("/").rsplit("/", 1)[-1]
        headers["Location"] = f"/api/v1/cameras/{camera_id}/whep/{session}"
    return Response(
        resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers=headers,
    )


@router.api_route("/{camera_id}/whep/{session}", methods=["PATCH", "DELETE"])
async def whep_session(
    camera_id: str, session: str, request: Request, _user: CurrentUser, ctx: Ctx
) -> Response:
    row = await _row(ctx, camera_id)
    if not session.replace("-", "").isalnum():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "некорректная сессия")
    url = f"{_whep_base(ctx, row)}/{session}"
    try:
        resp = await ctx.http.request(
            request.method,
            url,
            content=await request.body(),
            headers={
                "Content-Type": request.headers.get(
                    "content-type", "application/trickle-ice-sdpfrag"
                )
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"MediaMTX недоступен: {type(exc).__name__}"
        ) from exc
    return Response(
        resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type")
    )
