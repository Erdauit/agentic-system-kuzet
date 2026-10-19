"""Подключение к Postgres (SQLAlchemy 2 async + asyncpg).

asyncpg выбран вместо psycopg: у psycopg лицензия LGPL, её нет в белом списке (SPEC.md §2.7).
Требует extra `qoima-shared[postgres]`.
"""

from __future__ import annotations

import os
import time

from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.storage.secrets import read_secret

ENV_URL = "QOIMA_DATABASE_URL"


def database_url() -> URL:
    """URL БД: $QOIMA_DATABASE_URL (разработка, тесты) или части из окружения
    + пароль из секрета."""
    if raw := os.environ.get(ENV_URL):
        url = make_url(raw)
        if url.drivername in {"postgresql", "postgres"}:
            url = url.set(drivername="postgresql+asyncpg")
        return url
    password = read_secret("postgres_password", path_env="QOIMA_DB_PASSWORD_FILE")
    return URL.create(
        drivername="postgresql+asyncpg",
        username=os.environ.get("QOIMA_DB_USER", "qoima"),
        password=password.get_secret_value(),
        host=os.environ.get("QOIMA_DB_HOST", "postgres"),
        port=int(os.environ.get("QOIMA_DB_PORT", "5432")),
        database=os.environ.get("QOIMA_DB_NAME", "qoima"),
    )


def create_engine(url: URL | None = None) -> AsyncEngine:
    # pool_pre_ping: после рестарта Postgres первый запрос не должен падать на мёртвом соединении.
    return create_async_engine(url or database_url(), pool_pre_ping=True)


async def ping(engine: AsyncEngine) -> float:
    """SELECT 1; возвращает задержку в миллисекундах."""
    started = time.perf_counter()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return (time.perf_counter() - started) * 1000
