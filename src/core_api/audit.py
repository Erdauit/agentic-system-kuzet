"""Запись в аудит-лог (SPEC.md §3.3, §10): кто, что, когда. Таблица только для добавления."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.db import models
from shared.telemetry.logging import redact_value


async def write_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Добавляет запись в текущую транзакцию. Секреты в details маскируются."""
    session.add(
        models.AuditLog(
            kind="user_action",
            actor=actor[:120],
            action=action[:80],
            target=target[:200] if target else None,
            payload=redact_value("details", details or {}),
        )
    )
