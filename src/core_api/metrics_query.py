"""Запросы к Prometheus по белому списку шаблонов (история камер, SLO).

Произвольный PromQL снаружи не принимается: шаблон выбирается по имени, параметры — только
идентификатор камеры (проверяется по формату) и диапазон из фиксированного набора.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

_CAMERA = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

CAMERA_TEMPLATES: dict[str, str] = {
    "fps": 'camera_fps_in{{camera="{camera}"}}',
    "sharpness": 'camera_sharpness{{camera="{camera}"}}',
    "frozen": 'camera_frozen{{camera="{camera}"}}',
    "view_shift": 'camera_view_shift_score{{camera="{camera}"}}',
    "reconnects": 'increase(camera_reconnects_total{{camera="{camera}"}}[10m])',
    "decode_errors": 'increase(decode_errors_total{{camera="{camera}"}}[10m])',
}

# Диапазон → шаг: ~150–170 точек на графике, этого достаточно для карточки камеры.
RANGES: dict[str, tuple[int, int]] = {"24h": (86400, 600), "7d": (604800, 3600)}

SLO_OFFLINE_P95 = (
    "histogram_quantile(0.95, sum(rate(camera_offline_detection_seconds_bucket[7d])) by (le))"
)


class QueryError(Exception):
    pass


@dataclass(frozen=True)
class Series:
    points: list[tuple[float, float]]


class PrometheusClient:
    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self.url = url.rstrip("/")
        self.http = http

    async def camera_history(self, camera_id: str, metric: str, range_name: str) -> Series:
        if not _CAMERA.match(camera_id):
            raise QueryError("некорректный идентификатор камеры")
        template = CAMERA_TEMPLATES.get(metric)
        rng = RANGES.get(range_name)
        if template is None or rng is None:
            raise QueryError("неизвестная метрика или диапазон")
        span, step = rng
        end = time.time()
        body = await self._get(
            "/api/v1/query_range",
            {
                "query": template.format(camera=camera_id),
                "start": end - span,
                "end": end,
                "step": step,
            },
        )
        result = body.get("data", {}).get("result", [])
        points: list[tuple[float, float]] = []
        if result:
            points = [
                (float(t), float(v))
                for t, v in result[0].get("values", [])
                if v not in ("NaN", "+Inf")
            ]
        return Series(points=points)

    async def scalar(self, query: str) -> float | None:
        body = await self._get("/api/v1/query", {"query": query})
        result = body.get("data", {}).get("result", [])
        if not result:
            return None
        value = result[0].get("value", [None, None])[1]
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return None if v != v else v  # NaN → нет данных

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.http.get(f"{self.url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise QueryError(f"Prometheus недоступен: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise QueryError(f"Prometheus ответил HTTP {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data
