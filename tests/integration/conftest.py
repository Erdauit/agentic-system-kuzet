"""Интеграционные тесты: нужны реальные Postgres и Valkey.

    QOIMA_TEST_DATABASE_URL=postgresql+asyncpg://qoima:...@127.0.0.1:5432/qoima_test
    QOIMA_TEST_VALKEY_URL=valkey://127.0.0.1:6379/0

Без переменных тесты пропускаются (в CI они заданы через services).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("QOIMA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("QOIMA_TEST_DATABASE_URL не задан")
    return url


@pytest.fixture
def valkey_url() -> str:
    url = os.environ.get("QOIMA_TEST_VALKEY_URL")
    if not url:
        pytest.skip("QOIMA_TEST_VALKEY_URL не задан")
    return url
