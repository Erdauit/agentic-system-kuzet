"""Миграции на реальном Postgres: upgrade → схема совпадает с ORM → downgrade → upgrade."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from core_api.db.migrate import alembic_config, head_revision
from core_api.db.models import Base

pytestmark = pytest.mark.integration


def _diff(sync_conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, Base.metadata))


async def _run(url: str, *statements: str) -> None:
    """asyncpg не принимает несколько команд в одном запросе — выполняем по одной."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for sql in statements:
                await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _diff_async(url: str) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_diff)
    finally:
        await engine.dispose()


def test_upgrade_matches_models_and_downgrade_works(database_url: str) -> None:
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert asyncio.run(_diff_async(database_url)) == [], "ORM и миграции разошлись"

    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert head_revision() == "0001"


def test_database_enforces_invariants(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
    asyncio.run(
        _run(database_url, "DELETE FROM feedback", "DELETE FROM events", "DELETE FROM incidents")
    )

    # Человек в контуре на уровне БД: act/ok без подтверждения не записывается.
    with pytest.raises(IntegrityError):
        asyncio.run(
            _run(
                database_url,
                "INSERT INTO audit_log (kind, actor, action, permission, status) "
                "VALUES ('tool_call', 'chat', 'promote_model', 'act', 'ok')",
            )
        )

    # Аудит-лог только для добавления.
    asyncio.run(
        _run(
            database_url,
            "INSERT INTO audit_log (kind, actor, action) VALUES ('user_action', 't', 'login')",
        )
    )
    with pytest.raises(DBAPIError, match="только для добавления"):
        asyncio.run(_run(database_url, "UPDATE audit_log SET actor = 'x'"))

    # Состояние инцидента согласовано с отметками времени.
    with pytest.raises(IntegrityError):
        asyncio.run(
            _run(
                database_url,
                "INSERT INTO incidents (id, type, state, severity, camera_ids, dedup_key, opened_at) "
                f"VALUES ('{uuid.uuid4()}', 'fall', 'ACKNOWLEDGED', 'critical', '[\"c\"]', 'k', now())",
            )
        )

    # Только одна активная версия модели.
    insert_model = (
        "INSERT INTO models (name, version, task_type, framework, license, weights_sha256, source, status) "
        "VALUES ('m', '{v}', 'detection', 'rf-detr', 'Apache-2.0', '{h}', 'pretrained', 'active')"
    )
    asyncio.run(_run(database_url, "DELETE FROM models"))
    asyncio.run(_run(database_url, insert_model.format(v="1.0.0", h="a" * 64)))
    with pytest.raises(IntegrityError):
        asyncio.run(_run(database_url, insert_model.format(v="1.1.0", h="b" * 64)))
    asyncio.run(_run(database_url, "DELETE FROM models"))
