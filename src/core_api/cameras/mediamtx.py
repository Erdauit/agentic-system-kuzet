"""Синхронизация путей MediaMTX с таблицей камер.

Источник правды — БД core-api. Пути, добавленные через API MediaMTX, живут только в памяти
MediaMTX и пропадают при его рестарте, поэтому синхронизатор запускается при старте core-api,
после каждого изменения камер и периодически.

Для каждой включённой камеры: `<id>/main` и `<id>/sub` — MediaMTX постоянно тянет поток с
камеры (sourceOnDemand: false: Camera Health должен видеть поток всегда) по TCP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from shared.telemetry import get_logger

log = get_logger("mediamtx")

# Пути, которыми управляет Qoima: <camera_id>/main|sub. Чужие пути не трогаем.
MANAGED_PATH = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}/(main|sub)$")


def main_path(camera_id: str) -> str:
    return f"{camera_id}/main"


def sub_path(camera_id: str) -> str:
    return f"{camera_id}/sub"


@dataclass(frozen=True)
class DesiredPath:
    name: str
    source: str  # rtsp://user:pass@camera/... — содержит пароль, не логировать

    def config(self) -> dict[str, Any]:
        return {"source": self.source, "sourceOnDemand": False, "rtspTransport": "tcp"}


class MediaMtxClient:
    def __init__(self, api_url: str, http: httpx.AsyncClient) -> None:
        self.api = api_url.rstrip("/")
        self.http = http

    async def list_paths(self) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        page = 0
        while True:
            resp = await self.http.get(
                f"{self.api}/v3/config/paths/list", params={"page": page, "itemsPerPage": 200}
            )
            resp.raise_for_status()
            body = resp.json()
            for item in body.get("items") or []:
                items[str(item.get("name"))] = item
            page += 1
            if page >= int(body.get("pageCount") or 1):
                return items

    async def add(self, path: DesiredPath) -> None:
        resp = await self.http.post(
            f"{self.api}/v3/config/paths/add/{path.name}", json=path.config()
        )
        resp.raise_for_status()

    async def replace(self, path: DesiredPath) -> None:
        resp = await self.http.post(
            f"{self.api}/v3/config/paths/replace/{path.name}", json=path.config()
        )
        resp.raise_for_status()

    async def delete(self, name: str) -> None:
        resp = await self.http.delete(f"{self.api}/v3/config/paths/delete/{name}")
        if resp.status_code != 404:
            resp.raise_for_status()

    async def reconcile(self, desired: list[DesiredPath]) -> dict[str, int]:
        current = await self.list_paths()
        want = {p.name: p for p in desired}
        stats = {"added": 0, "replaced": 0, "deleted": 0}
        for name, path in want.items():
            existing = current.get(name)
            if existing is None:
                await self.add(path)
                stats["added"] += 1
            elif (
                existing.get("source") != path.source or existing.get("sourceOnDemand") is not False
            ):
                await self.replace(path)
                stats["replaced"] += 1
        for name in current:
            if MANAGED_PATH.match(name) and name not in want:
                await self.delete(name)
                stats["deleted"] += 1
        if any(stats.values()):
            log.info("mediamtx_reconciled", **stats)
        return stats
