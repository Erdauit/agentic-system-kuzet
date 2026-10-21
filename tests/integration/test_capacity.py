"""План мощности на реальном Postgres: расчёт по замеру из каталога данных, дедупликация плана,
применение (FPS анализа камер → реестр), предупреждение при камерах сверх плана."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core_api.capacity.service import CapacityError, CapacityService
from core_api.db import models
from core_api.db.migrate import alembic_config
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import FitVerdict, PlanStatus
from tests.contract.examples import EXAMPLES

pytestmark = pytest.mark.integration


class FakeCameras:
    """Нужное CapacityService от CameraService: список камер и публикация реестра."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.published = 0

    async def list_all(self) -> list[models.Camera]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(models.Camera).order_by(models.Camera.id))).all()
        return [r[0] if isinstance(r[0], models.Camera) else _row(r) for r in rows]

    async def publish_registry(self) -> None:
        self.published += 1


def _row(r: Any) -> models.Camera:
    return models.Camera(**r._mapping)


@pytest.fixture
def migrated(database_url: str) -> str:
    # Синхронно и вне event loop: env.py Alembic сам запускает asyncio.
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated)
    async with eng.begin() as conn:
        for table in (
            models.AlertDeliveryRow,
            models.Feedback,
            models.Event,
            models.Incident,
            models.Zone,
            models.SystemAlert,
            models.CapacityPlanRow,
            models.Camera,
        ):
            await conn.execute(delete(table))
    yield eng
    async with eng.begin() as conn:
        await conn.execute(delete(models.CapacityPlanRow))
        await conn.execute(delete(models.Camera))
    await eng.dispose()


def bench_file(root: Path, det_thr: float) -> None:
    data = json.loads(json.dumps(EXAMPLES["BenchResult"]))
    data["models"] = [
        {
            **data["models"][0],
            "batch": 8,
            "throughput": det_thr,
            "latency_p95_ms": 20,
            "latency_p50_ms": 15,
        },
    ]
    (root / "bench").mkdir(parents=True, exist_ok=True)
    (root / "bench" / f"{data['id']}.json").write_text(json.dumps(data))


async def add_cameras(
    engine: AsyncEngine, n: int, scenarios: list[str], prefix: str = "cam"
) -> None:
    probe = {
        "role": "sub",
        "url": "rtsp://10.0.0.1/s",
        "codec": "h264",
        "width": 640,
        "height": 360,
        "fps": 15,
        "bitrate_kbps": 512,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    async with engine.begin() as conn:
        for i in range(n):
            await conn.execute(
                models.Camera.__table__.insert().values(
                    id=f"{prefix}-{i:02d}",
                    name=f"{prefix} {i}",
                    source="manual",
                    main_url="rtsp://10.0.0.1/m",
                    scenarios=scenarios,
                    probes=[probe],
                )
            )


async def test_compute_apply_and_drift(
    engine: AsyncEngine, config: QoimaConfig, tmp_path: Path
) -> None:
    # Детектор 40 изобр/с: 4 камеры падения влезают на целевом FPS, 8 — только на пониженном.
    bench_file(tmp_path, det_thr=40.0)
    cams = FakeCameras(engine)
    svc = CapacityService(engine, lambda: config, tmp_path, cams, Broadcaster())  # type: ignore[arg-type]
    await add_cameras(engine, 4, ["fall"])
    plan, _ = await svc.compute()
    stored = await svc.save_draft(plan, "site_admin:test")
    again = await svc.save_draft((await svc.compute())[0], "site_admin:test")
    assert again.plan.id == stored.plan.id  # те же входы — тот же план, без дублей
    assert stored.status is PlanStatus.DRAFT and plan.verdict is FitVerdict.FITS_WITH_LIMITATIONS
    applied = await svc.apply(plan.id, "site_admin:test")
    assert applied.status is PlanStatus.ACTIVE and cams.published == 1
    async with engine.connect() as conn:
        fps = dict((await conn.execute(select(models.Camera.id, models.Camera.analysis_fps))).all())
    assert set(fps.values()) == {None}  # всё на целевом FPS — значения сценариев

    st = await svc.status()
    assert not st.stale and st.changes == []

    # Добавили камеры сверх плана → предупреждение и предложение понизить FPS.
    await add_cameras(engine, 4, ["fall"], prefix="new")
    st = await svc.status()
    assert st.stale and st.draft is not None
    assert any("понизить FPS" in m for m in st.messages), st.messages
    assert st.changes and all(c.proposed < config.scenarios["fall"].target_fps for c in st.changes)  # type: ignore[index]
    assert st.active is not None and st.active.plan.id == plan.id  # само ничего не применилось

    second = await svc.save_draft(st.draft, "site_admin:test")
    await svc.apply(second.plan.id, "site_admin:test")
    old = await svc.get(plan.id)
    assert old is not None and old.status is PlanStatus.SUPERSEDED
    async with engine.connect() as conn:
        lowered = [
            f
            for (f,) in (await conn.execute(select(models.Camera.analysis_fps))).all()
            if f is not None
        ]
    assert lowered and all(f < config.scenarios["fall"].target_fps for f in lowered)  # type: ignore[index]


async def test_does_not_fit_cannot_be_applied(
    engine: AsyncEngine, config: QoimaConfig, tmp_path: Path
) -> None:
    bench_file(tmp_path, det_thr=5.0)
    svc = CapacityService(engine, lambda: config, tmp_path, FakeCameras(engine), Broadcaster())  # type: ignore[arg-type]
    await add_cameras(engine, 20, ["fall"])
    plan, _ = await svc.compute()
    assert plan.verdict is FitVerdict.DOES_NOT_FIT
    await svc.save_draft(plan, "site_admin:test")
    with pytest.raises(CapacityError, match="не подходит"):
        await svc.apply(plan.id, "site_admin:test")


async def test_no_bench(engine: AsyncEngine, config: QoimaConfig, tmp_path: Path) -> None:
    svc = CapacityService(engine, lambda: config, tmp_path, FakeCameras(engine), Broadcaster())  # type: ignore[arg-type]
    with pytest.raises(CapacityError, match="qoima bench"):
        await svc.compute()
    st = await svc.status()
    assert st.draft is None and "qoima bench" in st.messages[0]
