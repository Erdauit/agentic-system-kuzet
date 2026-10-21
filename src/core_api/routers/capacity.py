"""План мощности (мастер, шаг 6): замеры `qoima bench`, расчёт, применение, PDF, контроль.

Читать может любой вошедший (состояние системы показывает загрузку); считать и применять —
администратор. Применение плана меняет FPS анализа камер и пишется в аудит.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from core_api.api_models import ApiModel
from core_api.auth.deps import AdminUser, CurrentUser
from core_api.capacity.report import FontMissingError, render_pdf
from core_api.capacity.service import CapacityError, to_json
from core_api.context import AppContext, get_ctx
from shared.schemas import BenchResult

router = APIRouter(prefix="/api/v1/capacity", tags=["capacity"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


class ComputeIn(ApiModel):
    bench_id: str | None = None


def _bench_summary(b: BenchResult) -> dict[str, Any]:
    return {
        "id": b.id,
        "started_at": b.started_at.isoformat(),
        "finished_at": b.finished_at.isoformat(),
        "hostname": b.hardware.hostname,
        "gpus": [g.name for g in b.hardware.gpus],
        "decode": len(b.decode),
        "models": sorted({m.model for m in b.models}),
        "vlm": bool(b.vlm),
        "warnings": b.warnings,
    }


@router.get("/bench")
async def list_bench(_user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return [_bench_summary(b) for b in await asyncio.to_thread(ctx.capacity.bench_results)]


@router.get("/bench/{bench_id}", response_model=BenchResult)
async def get_bench(bench_id: str, _user: CurrentUser, ctx: Ctx) -> BenchResult:
    b = await asyncio.to_thread(ctx.capacity.bench, bench_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "замер не найден")
    return b


@router.get("/status")
async def capacity_status(_user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    st = await ctx.capacity.status()
    return {
        "bench_id": st.bench_id,
        "active": to_json(st.active),
        "draft": st.draft.model_dump(mode="json") if st.draft else None,
        "stale": st.stale,
        "changes": [
            {"camera_id": c.camera_id, "current": c.current, "proposed": c.proposed}
            for c in st.changes
        ],
        "messages": st.messages,
    }


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def compute_plan(body: ComputeIn, user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    try:
        plan, _ = await ctx.capacity.compute(body.bench_id)
    except CapacityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    stored = await ctx.capacity.save_draft(plan, user.actor)
    out = to_json(stored)
    return out or {}


@router.get("/plans")
async def plan_history(_user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    return [x for x in (to_json(p) for p in await ctx.capacity.history()) if x]


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: uuid.UUID, _user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    stored = await ctx.capacity.get(plan_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "план не найден")
    return to_json(stored) or {}


@router.post("/plans/{plan_id}/apply")
async def apply_plan(plan_id: uuid.UUID, user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    try:
        stored = await ctx.capacity.apply(plan_id, user.actor)
    except CapacityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await ctx.refresh_camera_names()
    return to_json(stored) or {}


@router.get("/plans/{plan_id}/report.pdf")
async def plan_pdf(plan_id: uuid.UUID, _user: CurrentUser, ctx: Ctx) -> Response:
    stored = await ctx.capacity.get(plan_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "план не найден")
    bench = await asyncio.to_thread(ctx.capacity.bench, stored.plan.benchmark_id)
    try:
        pdf = await asyncio.to_thread(
            render_pdf, stored.plan, bench, ctx.config.defaults.site.name, ctx.camera_names
        )
    except FontMissingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    name = f"qoima-capacity-{stored.plan.created_at:%Y%m%d}-{str(plan_id)[:8]}.pdf"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
