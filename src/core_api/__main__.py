"""Точка входа контейнера core-api: миграции → uvicorn (TLS, если заданы сертификаты)."""

from __future__ import annotations

import uvicorn

from core_api.app import create_app
from core_api.db.migrate import upgrade_head
from core_api.settings import Settings
from shared.telemetry import configure_logging, get_logger


def main() -> None:
    settings = Settings.from_env()
    configure_logging("core-api", settings.log_level)
    log = get_logger("core-api")
    if settings.run_migrations:
        # Синхронно и до старта event loop: env.py Alembic сам запускает asyncio.
        log.info("migrations_start")
        upgrade_head()
        log.info("migrations_done")
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        ssl_certfile=str(settings.tls_cert_file) if settings.tls_cert_file else None,
        ssl_keyfile=str(settings.tls_key_file) if settings.tls_key_file else None,
        log_config=None,  # логи идут через structlog (shared.telemetry.logging)
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
