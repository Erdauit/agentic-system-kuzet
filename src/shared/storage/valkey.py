"""Подключение к Valkey (очереди на Streams, кэш, pub/sub). Redis не используется (SPEC.md §2.7).

Требует extra `qoima-shared[valkey]`.
"""

from __future__ import annotations

import os
import time

from valkey.asyncio import Valkey

from shared.storage.secrets import SecretNotFoundError, read_secret

ENV_URL = "QOIMA_VALKEY_URL"


def create_client() -> Valkey:
    """Клиент: $QOIMA_VALKEY_URL (разработка, тесты) или хост/порт из окружения
    + пароль из секрета."""
    if url := os.environ.get(ENV_URL):
        client: Valkey = Valkey.from_url(url)
        return client
    try:
        password: str | None = read_secret(
            "valkey_password", path_env="QOIMA_VALKEY_PASSWORD_FILE"
        ).get_secret_value()
    except SecretNotFoundError:
        password = None
    return Valkey(
        host=os.environ.get("QOIMA_VALKEY_HOST", "valkey"),
        port=int(os.environ.get("QOIMA_VALKEY_PORT", "6379")),
        password=password,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def ping(client: Valkey) -> float:
    started = time.perf_counter()
    if not await client.ping():
        raise ConnectionError("Valkey не ответил на PING")
    return (time.perf_counter() - started) * 1000
