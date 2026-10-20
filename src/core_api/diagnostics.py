"""Состояние камер для `qoima doctor` (запускается в контейнере core-api через docker exec).

Печатает JSON: id, name, status, detail, recommendation — по последнему снимку Camera Health
из Valkey. Нет снимка — камера «нет данных».
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select

from core_api.alerts.system import ISSUE_ALERTS, camera_message
from core_api.db import models
from shared.schemas import CameraHealth
from shared.storage import postgres, valkey

LATEST_KEY = "qoima:camera_health:latest:{camera}"

RECOMMENDATIONS = {
    "stream_down": "Проверьте питание и сеть камеры, затем адрес потока в мастере",
    "frozen": "Перезагрузите камеру",
    "black": "Проверьте ИК-подсветку и не закрыт ли объектив",
    "overexposed": "Проверьте экспозицию, уберите прямой свет",
    "blur": "Протрите объектив, проверьте фокус и конденсат",
    "occluded": "Уберите препятствие перед камерой",
    "view_shift": "Верните камеру на место или переснимите базовую линию в мастере",
    "low_fps": "Проверьте сеть до камеры и её настройки FPS",
}


async def collect() -> list[dict[str, str]]:
    engine = postgres.create_engine()
    client = valkey.create_client()
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(select(models.Camera).where(models.Camera.enabled))).all()
        out = []
        for row in rows:
            cam = row[0] if isinstance(row, tuple) else row
            raw = await client.get(LATEST_KEY.format(camera=cam.id))
            if raw is None:
                out.append(
                    {
                        "id": cam.id,
                        "name": cam.name,
                        "status": "unknown",
                        "detail": "нет данных Camera Health",
                        "recommendation": "Проверьте vision-pipeline: qoima status",
                    }
                )
                continue
            health = CameraHealth.model_validate_json(raw)
            issues = [i.value for i in health.issues]
            detail = "норма" if not issues else ", ".join(issues)
            for issue in health.issues:
                if issue in ISSUE_ALERTS:
                    detail = camera_message(ISSUE_ALERTS[issue][0], cam.name, cam.id, health)
                    break
            rec = next((RECOMMENDATIONS[i] for i in issues if i in RECOMMENDATIONS), "")
            out.append(
                {
                    "id": cam.id,
                    "name": cam.name,
                    "status": health.status.value,
                    "detail": detail,
                    "recommendation": rec,
                }
            )
        return out
    finally:
        await client.aclose()
        await engine.dispose()


def main() -> int:
    print(json.dumps(asyncio.run(collect()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
