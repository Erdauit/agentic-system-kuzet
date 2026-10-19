"""Клиенты shared.storage против реальных Postgres и Valkey."""

from __future__ import annotations

import pytest

from shared.storage import postgres, valkey

pytestmark = pytest.mark.integration


async def test_postgres_ping(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QOIMA_DATABASE_URL", database_url)
    engine = postgres.create_engine()
    try:
        assert await postgres.ping(engine) >= 0
    finally:
        await engine.dispose()


async def test_valkey_ping(valkey_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QOIMA_VALKEY_URL", valkey_url)
    client = valkey.create_client()
    try:
        assert await valkey.ping(client) >= 0
    finally:
        await client.aclose()
