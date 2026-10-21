"""План мощности в core-api: результаты `qoima bench`, расчёт, применение, контроль изменений.

- Замеры лежат в <data_root>/bench/bench-*.json — их пишет разовый контейнер бенчмарка.
- План считается по последнему замеру, текущим камерам и сценариям и по фактической серой зоне
  из событий (если статистики достаточно). Хранится в capacity_plans; применяет только человек.
- Применённый план задаёт камерам FPS анализа (cameras.analysis_fps → реестр → vision-pipeline).
- status(): если камеры или сценарии изменились, план пересчитывается и UI предупреждает,
  какие камеры нужно перевести на пониженный FPS (решение Фазы 3, п. 5: само ничего не меняет).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core_api.audit import write_audit
from core_api.cameras.service import CameraService
from core_api.capacity.planner import (
    CameraDemand,
    GrayStats,
    PlannerInputs,
    camera_fps,
    plan_capacity,
)
from core_api.db import models
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import (
    BenchResult,
    CapacityPlan,
    EventType,
    FitVerdict,
    PlanStatus,
    RoutingDecision,
    ScenarioKind,
    StreamProbe,
    StreamRole,
)
from shared.telemetry import get_logger

log = get_logger("capacity")

EVENT_SCENARIO = {
    EventType.FALL: ScenarioKind.FALL,
    EventType.RED_ZONE: ScenarioKind.RED_ZONE,
    EventType.SMOKE_FIRE: ScenarioKind.SMOKE_FIRE,
}
GRAY_SCORES_LIMIT = 2000


class CapacityError(Exception):
    """Нельзя посчитать или применить план (нет замеров, план не найден и т.п.)."""


@dataclass
class StoredPlan:
    plan: CapacityPlan
    status: PlanStatus
    created_by: str
    applied_at: datetime | None
    applied_by: str | None


@dataclass
class FpsChange:
    camera_id: str
    current: float | None
    proposed: float


@dataclass
class CapacityStatus:
    bench_id: str | None
    active: StoredPlan | None
    draft: CapacityPlan | None
    stale: bool
    changes: list[FpsChange] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class CapacityService:
    def __init__(
        self,
        engine: AsyncEngine,
        config: Callable[[], QoimaConfig],
        data_root: Path,
        cameras: CameraService,
        broadcaster: Broadcaster,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.config = config
        self.bench_dir = data_root / "bench"
        self.cameras = cameras
        self.broadcaster = broadcaster

    # ------------------------------------------------------------ замеры

    def bench_results(self) -> list[BenchResult]:
        """Все прочитанные замеры, новые первыми. Повреждённый файл пропускается с записью в лог."""
        out = []
        for path in sorted(self.bench_dir.glob("bench-*.json"), reverse=True):
            try:
                out.append(BenchResult.model_validate_json(path.read_text()))
            except (OSError, ValidationError) as exc:
                log.warning("bench_file_invalid", file=path.name, error=str(exc)[:300])
        return out

    def bench(self, bench_id: str | None = None) -> BenchResult | None:
        results = self.bench_results()
        if bench_id is None:
            return results[0] if results else None
        return next((b for b in results if b.id == bench_id), None)

    # ------------------------------------------------------------ входы планировщика

    async def demands(self) -> tuple[list[CameraDemand], list[str]]:
        out, warnings = [], []
        for row in await self.cameras.list_all():
            if not row.enabled:
                continue
            probes = [StreamProbe.model_validate(p) for p in row.probes or []]
            analysis = next((p for p in probes if p.role is StreamRole.SUB), None) or next(
                (p for p in probes if p.role is StreamRole.MAIN), None
            )
            if analysis is None:
                warnings.append(f"{row.id}: поток не зондирован — камера не учтена в плане")
                continue
            out.append(
                CameraDemand(
                    camera_id=row.id,
                    scenarios=tuple(ScenarioKind(s) for s in row.scenarios),
                    codec=analysis.codec.value,
                    width=analysis.width,
                    height=analysis.height,
                    native_fps=analysis.fps,
                )
            )
        return out, warnings

    async def gray_stats(self, demands: list[CameraDemand]) -> dict[ScenarioKind, GrayStats]:
        """Серая зона по фактическим событиям: запросы к арбитру за gray_stats_days."""
        days = self.config().defaults.capacity.gray_stats_days
        since = datetime.now(UTC) - timedelta(days=days)
        stats: dict[ScenarioKind, GrayStats] = {}
        async with self.sessions() as s:
            for etype, scen in EVENT_SCENARIO.items():
                cams = sum(1 for d in demands if scen in d.scenarios)
                if cams == 0:
                    continue
                cond = (
                    models.Event.type == etype.value,
                    models.Event.routing == RoutingDecision.EVENT_WITH_ARBITER.value,
                    models.Event.synthetic.is_(None),
                    models.Event.detected_at >= since,
                )
                count = int(
                    await s.scalar(select(func.count()).select_from(models.Event).where(*cond)) or 0
                )
                if count == 0:
                    continue
                scores = list(
                    await s.scalars(
                        select(models.Event.detector_score)
                        .where(*cond)
                        .order_by(models.Event.detected_at.desc())
                        .limit(GRAY_SCORES_LIMIT)
                    )
                )
                stats[scen] = GrayStats(
                    events=count, camera_hours=cams * days * 24.0, scores=tuple(scores)
                )
        return stats

    async def compute(self, bench_id: str | None = None) -> tuple[CapacityPlan, list[str]]:
        bench = self.bench(bench_id)
        if bench is None:
            raise CapacityError("нет результатов бенчмарка: на сервере выполните qoima bench")
        demands, warnings = await self.demands()
        gray = await self.gray_stats(demands)
        plan = plan_capacity(
            PlannerInputs(bench, tuple(demands), gray), self.config(), datetime.now(UTC)
        )
        if warnings:
            plan = plan.model_copy(update={"warnings": [*warnings, *plan.warnings]})
        return plan, warnings

    # ------------------------------------------------------------ хранение и применение

    async def save_draft(self, plan: CapacityPlan, actor: str) -> StoredPlan:
        async with self.sessions() as s:
            row = await s.get(models.CapacityPlanRow, plan.id)
            if row is None:  # тот же план (те же входы) не дублируется
                row = models.CapacityPlanRow(
                    id=plan.id,
                    created_by=actor,
                    benchmark_id=plan.benchmark_id,
                    status=PlanStatus.DRAFT.value,
                    verdict=plan.verdict.value,
                    plan=plan.model_dump(mode="json"),
                )
                s.add(row)
                await write_audit(
                    s,
                    actor=actor,
                    action="capacity_plan_computed",
                    target=f"capacity_plan:{plan.id}",
                    details={"verdict": plan.verdict.value, "benchmark": plan.benchmark_id},
                )
                await s.commit()
            return _stored(row)

    async def get(self, plan_id: uuid.UUID) -> StoredPlan | None:
        async with self.sessions() as s:
            row = await s.get(models.CapacityPlanRow, plan_id)
            return _stored(row) if row else None

    async def active(self) -> StoredPlan | None:
        async with self.sessions() as s:
            row = await s.scalar(
                select(models.CapacityPlanRow).where(
                    models.CapacityPlanRow.status == PlanStatus.ACTIVE.value
                )
            )
            return _stored(row) if row else None

    async def history(self, limit: int = 20) -> list[StoredPlan]:
        async with self.sessions() as s:
            rows = await s.scalars(
                select(models.CapacityPlanRow)
                .order_by(models.CapacityPlanRow.created_at.desc())
                .limit(limit)
            )
            return [_stored(r) for r in rows]

    async def apply(self, plan_id: uuid.UUID, actor: str) -> StoredPlan:
        """Применить план: FPS анализа камер → реестр → vision-pipeline. Только по действию человека."""
        now = datetime.now(UTC)
        async with self.sessions() as s:
            row = await s.get(models.CapacityPlanRow, plan_id, with_for_update=True)
            if row is None:
                raise CapacityError("план не найден")
            plan = CapacityPlan.model_validate(row.plan)
            if plan.verdict is FitVerdict.DOES_NOT_FIT:
                raise CapacityError(
                    "план «не подходит»: применять нельзя — уменьшите число камер или сценариев, "
                    "либо добавьте GPU и повторите qoima bench"
                )
            await s.execute(
                update(models.CapacityPlanRow)
                .where(
                    models.CapacityPlanRow.status == PlanStatus.ACTIVE.value,
                    models.CapacityPlanRow.id != plan_id,
                )
                .values(status=PlanStatus.SUPERSEDED.value)
            )
            row.status = PlanStatus.ACTIVE.value
            row.applied_at, row.applied_by = now, actor
            cfg = self.config()
            fps = camera_fps(plan)
            targets: dict[str, float] = {}
            for a in plan.allocations:
                targets[a.camera_id] = max(targets.get(a.camera_id, 0.0), a.target_fps)
            changed: dict[str, float | None] = {}
            for cam in await s.scalars(select(models.Camera)):
                planned = fps.get(cam.id)
                # Пониженный FPS — явное значение; на целевом FPS — NULL (значения сценариев).
                value = (
                    planned
                    if planned is not None and planned < targets.get(cam.id, 0) - 1e-9
                    else None
                )
                if cam.analysis_fps != value:
                    cam.analysis_fps = value
                    changed[cam.id] = value
            await write_audit(
                s,
                actor=actor,
                action="capacity_plan_applied",
                target=f"capacity_plan:{plan_id}",
                details={
                    "verdict": plan.verdict.value,
                    "analysis_fps": changed,
                    "backend": cfg.defaults.pipeline.backend,
                },
            )
            await s.commit()
            stored = _stored(row)
        await self.cameras.publish_registry()
        self.broadcaster.publish({"type": "capacity", "event": "applied", "plan_id": str(plan_id)})
        log.info("capacity_plan_applied", plan=str(plan_id), cameras_changed=len(changed))
        return stored

    # ------------------------------------------------------------ контроль изменений

    async def status(self) -> CapacityStatus:
        bench = self.bench()
        active = await self.active()
        if bench is None:
            return CapacityStatus(
                None,
                active,
                None,
                stale=active is not None,
                messages=["нет результатов бенчмарка: на сервере выполните qoima bench"],
            )
        bench_id = (
            active.plan.benchmark_id
            if active and self.bench(active.plan.benchmark_id)
            else bench.id
        )
        draft, _ = await self.compute(bench_id)
        messages: list[str] = []
        changes: list[FpsChange] = []
        if active is None:
            messages.append("план мощности не применён — FPS камер по умолчанию сценариев")
            stale = True
        else:
            current = camera_fps(active.plan)
            for cam, f in sorted(camera_fps(draft).items()):
                if abs(current.get(cam, -1) - f) > 1e-9:
                    changes.append(FpsChange(cam, current.get(cam), f))
            # Устарел — если поменялись камеры/сценарии, FPS или вердикт. Сдвиг одной лишь
            # статистики серой зоны (каждый день новые события) плашку не поднимает.
            stale = (
                _allocation_keys(draft) != _allocation_keys(active.plan)
                or bool(changes)
                or draft.verdict is not active.plan.verdict
            )
            if stale:
                messages.append(
                    "камеры или сценарии изменились после применения плана — план пересчитан"
                )
        if bench.id != bench_id:
            messages.append(f"есть более новый замер {bench.id} — пересчитайте план по нему")
        if draft.verdict is FitVerdict.DOES_NOT_FIT:
            messages.append("камер больше, чем вмещает этот сервер: см. предупреждения плана")
        elif any(a.degraded for a in draft.allocations) and changes:
            messages.append(
                "чтобы всё поместилось, у части камер нужно понизить FPS — примените пересчитанный план"
            )
        return CapacityStatus(bench.id, active, draft, stale, changes, messages)


def _allocation_keys(plan: CapacityPlan) -> set[tuple[str, str]]:
    return {(a.camera_id, a.scenario.value) for a in plan.allocations}


def _stored(row: models.CapacityPlanRow) -> StoredPlan:
    return StoredPlan(
        plan=CapacityPlan.model_validate(row.plan),
        status=PlanStatus(row.status),
        created_by=row.created_by,
        applied_at=row.applied_at,
        applied_by=row.applied_by,
    )


def to_json(stored: StoredPlan | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    return {
        "plan": stored.plan.model_dump(mode="json"),
        "status": stored.status.value,
        "created_by": stored.created_by,
        "applied_at": stored.applied_at.isoformat() if stored.applied_at else None,
        "applied_by": stored.applied_by,
    }
