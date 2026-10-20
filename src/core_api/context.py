"""Зависимости приложения core-api, доступные маршрутам через request.app.state.ctx."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine
from valkey.asyncio import Valkey

from core_api.alerts.channels import ChannelDispatcher
from core_api.alerts.system import SystemAlertManager
from core_api.auth.service import AuthService
from core_api.cameras.jobs import DiscoveryJobs
from core_api.cameras.service import CameraService
from core_api.metrics_query import PrometheusClient
from core_api.realtime import Broadcaster
from core_api.settings import Settings
from core_api.site_config import SiteConfigStore
from shared.config import QoimaConfig
from shared.storage import LocalFSBlobStore


@dataclass
class AppContext:
    settings: Settings
    config: QoimaConfig
    engine: AsyncEngine
    valkey: Valkey
    http: httpx.AsyncClient
    blobs: LocalFSBlobStore
    auth: AuthService
    cameras: CameraService
    alerts: SystemAlertManager
    dispatcher: ChannelDispatcher
    broadcaster: Broadcaster
    discovery: DiscoveryJobs
    site: SiteConfigStore
    prometheus: PrometheusClient | None
    data_root: Path
    runtime_secrets: Path
    camera_names: dict[str, str] = field(default_factory=dict)

    def get_config(self) -> QoimaConfig:
        return self.config

    async def refresh_camera_names(self) -> None:
        self.camera_names = {
            row.id: row.name for row in await self.cameras.list_all() if row.enabled
        }


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx
