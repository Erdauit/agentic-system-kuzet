"""Сервис камер: подключение (ручное, из автопоиска, импорт), базовая линия, MediaMTX, реестр.

Порядок подключения камеры:
1. зондирование кандидатов → выбор main и substream;
2. снапшот и базовая линия качества с потока анализа (substream);
3. учётные данные → .secrets/runtime, запись в БД, запись в аудит;
4. синхронизация путей MediaMTX и публикация реестра для vision-pipeline.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from core_api.audit import write_audit
from core_api.cameras.credentials import CredentialStore
from core_api.cameras.discovery import strip_credentials
from core_api.cameras.mediamtx import DesiredPath, MediaMtxClient, main_path, sub_path
from core_api.cameras.onvif import Credentials
from core_api.cameras.probe import (
    ProbedCandidate,
    ProbeError,
    ProbeResult,
    baseline_from_gray,
    choose_streams,
    probe_candidates,
    with_credentials,
)
from core_api.db import models
from shared.config import QoimaConfig
from shared.schemas import (
    CameraConfig,
    CameraRegistryEntry,
    CameraSource,
    CandidateSource,
    DiscoveredDevice,
    ProbeReport,
    QualityBaseline,
    StreamCandidate,
    StreamProbe,
    StreamRole,
)
from shared.storage import LocalFSBlobStore
from shared.telemetry import get_logger

log = get_logger("cameras")

REGISTRY_KEY = "qoima:cameras"
REGISTRY_CHANNEL = "qoima:cameras:changed"


class CameraError(Exception):
    """Ошибка, понятная пользователю (показывается в мастере как есть)."""


@dataclass
class NewCamera:
    name: str
    main_url: str
    sub_url: str | None
    credentials: Credentials | None
    source: CameraSource
    location: str | None = None
    vendor: str | None = None
    model: str | None = None
    onvif_xaddr: str | None = None
    scenarios: list[str] = field(default_factory=list)


@dataclass
class CreatedCamera:
    camera: CameraConfig
    report: ProbeReport


def baseline_key(camera_id: str) -> str:
    return f"frames/{camera_id}/baseline.jpg"


def config_from_row(row: models.Camera) -> CameraConfig:
    return CameraConfig(
        id=row.id,
        name=row.name,
        source=CameraSource(row.source),
        main_url=row.main_url,
        sub_url=row.sub_url,
        credentials_secret=row.id if row.credentials_secret else None,
        enabled=row.enabled,
        scenarios=row.scenarios,
        location=row.location,
        vendor=row.vendor,
        model=row.model,
        onvif_xaddr=row.onvif_xaddr,
    )


def default_name(device: DiscoveredDevice, channel: int) -> str:
    base = device.name or " ".join(x for x in (device.manufacturer, device.model) if x) or "Камера"
    suffix = f" — канал {channel}" if device.channels > 1 else ""
    return f"{base} ({device.host}){suffix}"[:120]


class CameraService:
    def __init__(
        self,
        engine: AsyncEngine,
        valkey: Valkey,
        blobs: LocalFSBlobStore,
        credentials: CredentialStore,
        mediamtx: MediaMtxClient | None,
        config: Callable[[], QoimaConfig],
        mediamtx_rtsp_url: str,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.valkey = valkey
        self.blobs = blobs
        self.credentials = credentials
        self.mediamtx = mediamtx
        self.config = config
        self.mediamtx_rtsp_url = mediamtx_rtsp_url.rstrip("/")
        self.probe_semaphore = asyncio.Semaphore(config().defaults.probe.max_parallel)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ чтение

    async def list_all(self) -> list[models.Camera]:
        async with self.sessions() as s:
            return list((await s.scalars(select(models.Camera).order_by(models.Camera.id))).all())

    async def get(self, camera_id: str) -> models.Camera | None:
        async with self.sessions() as s:
            return await s.get(models.Camera, camera_id)

    # ------------------------------------------------------------ зондирование

    async def probe(
        self,
        host: str,
        candidates: list[StreamCandidate],
        creds: Credentials | None,
        channel: int = 1,
    ) -> tuple[ProbeReport, ProbeResult | None, ProbeResult | None]:
        cfg = self.config()
        selected = [c for c in candidates if c.channel == channel] or candidates
        probed: list[ProbedCandidate] = await probe_candidates(
            selected, creds, cfg.defaults.probe, self.probe_semaphore
        )
        return choose_streams(host, channel, probed, cfg.defaults.pipeline.substream_max_height)

    # ------------------------------------------------------------ подключение

    async def _next_id(self, session: AsyncSession) -> str:
        ids = set((await session.scalars(select(models.Camera.id))).all())
        n = len(ids) + 1
        while f"cam-{n:02d}" in ids:
            n += 1
        return f"cam-{n:02d}"

    def _baseline(self, camera_id: str, result: ProbeResult) -> QualityBaseline | None:
        if result.gray is None or result.snapshot_jpeg is None:
            return None
        width = self.config().defaults.camera_health.analysis_width
        sharpness, brightness, noise = baseline_from_gray(result.gray, width)
        self.blobs.put(baseline_key(camera_id), result.snapshot_jpeg)
        return QualityBaseline(
            sharpness=sharpness,
            brightness=brightness,
            noise=noise,
            snapshot_key=baseline_key(camera_id),
            captured_at=datetime.now(UTC),
        )

    async def create(self, new: NewCamera, actor: str) -> CreatedCamera:
        candidates = [
            StreamCandidate(
                url=new.main_url, source=CandidateSource.GENERIC, role_hint=StreamRole.MAIN
            )
        ]
        if new.sub_url and new.sub_url != new.main_url:
            candidates.append(
                StreamCandidate(
                    url=new.sub_url, source=CandidateSource.GENERIC, role_hint=StreamRole.SUB
                )
            )
        host = _host(new.main_url)
        report, main_r, sub_r = await self.probe(host, candidates, new.credentials)
        if main_r is None or report.main is None:
            errors = "; ".join(f"{t.url}: {t.error}" for t in report.tried if t.error)
            raise CameraError(f"поток камеры не открылся: {errors or 'нет данных'}")
        analysis = sub_r or main_r
        async with self._lock, self.sessions() as s:
            camera_id = await self._next_id(s)
            baseline = self._baseline(camera_id, analysis)
            if new.credentials is not None:
                self.credentials.save(camera_id, new.credentials)
            probes = [p.model_dump(mode="json") for p in (report.main, report.sub) if p is not None]
            row = models.Camera(
                id=camera_id,
                name=new.name[:120],
                source=new.source.value,
                main_url=strip_credentials(report.main.url),
                sub_url=strip_credentials(report.sub.url)
                if report.sub and report.sub.url != report.main.url
                else None,
                credentials_secret=camera_id if new.credentials is not None else None,
                enabled=True,
                scenarios=new.scenarios,
                location=new.location,
                probes=probes,
                baseline=baseline.model_dump(mode="json") if baseline else None,
                vendor=new.vendor,
                model=new.model,
                onvif_xaddr=new.onvif_xaddr,
            )
            s.add(row)
            await write_audit(
                s,
                actor=actor,
                action="camera_add",
                target=f"camera:{camera_id}",
                details={
                    "name": row.name,
                    "main_url": row.main_url,
                    "sub_url": row.sub_url,
                    "source": row.source,
                },
            )
            await s.commit()
        log.info("camera_added", camera=camera_id, source=new.source.value)
        await self.sync()
        return CreatedCamera(camera=config_from_row(row), report=report)

    async def add_discovered(
        self, device: DiscoveredDevice, creds: Credentials | None, actor: str
    ) -> list[CreatedCamera | CameraError]:
        """Все каналы найденного устройства (камера — 1 канал, NVR — несколько)."""
        results: list[CreatedCamera | CameraError] = []
        for channel in range(1, device.channels + 1):
            report, main_r, _sub_r = await self.probe(
                device.host, device.candidates, creds, channel
            )
            if report.main is None or main_r is None:
                errors = "; ".join(sorted({t.error for t in report.tried if t.error}))
                results.append(
                    CameraError(f"{device.host} канал {channel}: поток не открылся ({errors})")
                )
                continue
            try:
                created = await self.create(
                    NewCamera(
                        name=default_name(device, channel),
                        main_url=report.main.url,
                        sub_url=report.sub.url if report.sub else None,
                        credentials=creds,
                        source=CameraSource.ONVIF if device.onvif_xaddr else CameraSource.MANUAL,
                        vendor=device.manufacturer,
                        model=device.model,
                        onvif_xaddr=device.onvif_xaddr,
                    ),
                    actor,
                )
            except (CameraError, ProbeError) as exc:
                results.append(CameraError(str(exc)))
                continue
            results.append(created)
        return results

    async def existing_hosts(self) -> set[str]:
        return {_host(row.main_url) for row in await self.list_all()}

    async def update(self, camera_id: str, changes: dict[str, Any], actor: str) -> CameraConfig:
        allowed = {"name", "location", "enabled", "scenarios"}
        unknown = set(changes) - allowed
        if unknown:
            raise CameraError(f"нельзя изменить поля: {sorted(unknown)}")
        async with self.sessions() as s:
            row = await s.get(models.Camera, camera_id)
            if row is None:
                raise CameraError("камера не найдена")
            for key, value in changes.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            config_from_row(row)  # валидация контрактом до записи
            await write_audit(
                s,
                actor=actor,
                action="camera_change",
                target=f"camera:{camera_id}",
                details=changes,
            )
            await s.commit()
        await self.sync()
        return config_from_row(row)

    async def delete(self, camera_id: str, actor: str) -> None:
        async with self.sessions() as s:
            row = await s.get(models.Camera, camera_id)
            if row is None:
                raise CameraError("камера не найдена")
            events = (
                await s.execute(
                    select(func.count())
                    .select_from(models.Event)
                    .where(models.Event.camera_id == camera_id)
                )
            ).scalar_one()
            if events:
                raise CameraError(
                    "по камере есть события — отключите её вместо удаления (история сохранится)"
                )
            await s.delete(row)
            await write_audit(
                s, actor=actor, action="camera_delete", target=f"camera:{camera_id}", details={}
            )
            await s.commit()
        self.credentials.delete(camera_id)
        if self.blobs.exists(baseline_key(camera_id)):
            self.blobs.delete(baseline_key(camera_id))
        await self.sync()

    async def recapture_baseline(self, camera_id: str, actor: str) -> QualityBaseline:
        """Новая базовая линия по текущей картинке (после законной перестановки камеры)."""
        row = await self.get(camera_id)
        if row is None:
            raise CameraError("камера не найдена")
        path = sub_path(camera_id) if row.sub_url else main_path(camera_id)
        url = f"{self.mediamtx_rtsp_url}/{path}"
        candidates = [
            StreamCandidate(url=url, source=CandidateSource.GENERIC, role_hint=StreamRole.SUB)
        ]
        probed = await probe_candidates(
            candidates, None, self.config().defaults.probe, self.probe_semaphore
        )
        result = probed[0].result
        if result is None:
            raise CameraError(f"не удалось получить кадр: {probed[0].error}")
        baseline = self._baseline(camera_id, result)
        if baseline is None:
            raise CameraError("в потоке нет I-кадра для базовой линии")
        async with self.sessions() as s:
            db_row = await s.get(models.Camera, camera_id)
            if db_row is not None:
                db_row.baseline = baseline.model_dump(mode="json")
                db_row.updated_at = datetime.now(UTC)
            await write_audit(
                s, actor=actor, action="camera_baseline", target=f"camera:{camera_id}", details={}
            )
            await s.commit()
        await self.publish_registry()
        return baseline

    # ------------------------------------------------------------ синхронизация

    async def sync(self) -> None:
        await self.reconcile_mediamtx()
        await self.publish_registry()

    async def reconcile_mediamtx(self) -> None:
        if self.mediamtx is None:
            return
        desired: list[DesiredPath] = []
        for row in await self.list_all():
            if not row.enabled:
                continue
            creds = self.credentials.load(row.id)
            desired.append(DesiredPath(main_path(row.id), with_credentials(row.main_url, creds)))
            if row.sub_url:
                desired.append(DesiredPath(sub_path(row.id), with_credentials(row.sub_url, creds)))
        try:
            await self.mediamtx.reconcile(desired)
        except Exception as exc:  # MediaMTX недоступен — повторим при следующей синхронизации
            log.warning("mediamtx_reconcile_failed", error=str(exc))

    def registry(self, rows: list[models.Camera]) -> list[CameraRegistryEntry]:
        entries = []
        for row in rows:
            analysis_fps = None
            for p in row.probes or []:
                probe = StreamProbe.model_validate(p)
                if probe.role is StreamRole.SUB or (analysis_fps is None and not row.sub_url):
                    analysis_fps = probe.fps
            entries.append(
                CameraRegistryEntry(
                    id=row.id,
                    name=row.name,
                    enabled=row.enabled,
                    main_path=main_path(row.id),
                    sub_path=sub_path(row.id) if row.sub_url else None,
                    expected_fps=analysis_fps,
                    baseline=QualityBaseline.model_validate(row.baseline) if row.baseline else None,
                )
            )
        return entries

    async def publish_registry(self) -> None:
        entries = self.registry(await self.list_all())
        payload = json.dumps([e.model_dump(mode="json") for e in entries], ensure_ascii=False)
        try:
            await self.valkey.set(REGISTRY_KEY, payload)
            await self.valkey.publish(REGISTRY_CHANNEL, str(uuid.uuid4()))
        except Exception as exc:
            log.warning("registry_publish_failed", error=str(exc))


_HOST_RE = re.compile(r"^[a-z]+://(?:[^@/]*@)?(\[[^\]]+\]|[^:/?#]+)")


def _host(url: str) -> str:
    m = _HOST_RE.match(url)
    return m.group(1) if m else url
