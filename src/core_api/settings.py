"""Настройки процесса core-api из переменных окружения (адреса сервисов, TLS, порты).

Пороги и бизнес-параметры сюда не входят — они в configs/*.yaml (shared.config).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key, "").strip()
    return value or None


def _parse_service_urls(raw: str | None) -> dict[str, str]:
    """ "vision-pipeline=http://vision-pipeline:8080,agents=http://agents:8080" → словарь."""
    result: dict[str, str] = {}
    if not raw:
        return result
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ValueError(f"QOIMA_SERVICE_URLS: ожидается имя=url, получено {item!r}")
        result[name.strip()] = url.strip().rstrip("/")
    return result


@dataclass(frozen=True)
class Settings:
    bind_host: str = "0.0.0.0"  # noqa: S104 — сервис слушает внутри контейнера
    port: int = 8443
    log_level: str = "INFO"
    config_dir: Path | None = None
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    ui_dir: Path | None = None
    run_migrations: bool = True
    qdrant_url: str | None = None
    mediamtx_api_url: str | None = None
    triton_url: str | None = None
    service_urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        cert = _optional(e, "QOIMA_TLS_CERT_FILE")
        key = _optional(e, "QOIMA_TLS_KEY_FILE")
        if (cert is None) != (key is None):
            raise ValueError("QOIMA_TLS_CERT_FILE и QOIMA_TLS_KEY_FILE задаются вместе")
        config_dir = _optional(e, "QOIMA_CONFIG_DIR")
        ui_dir = _optional(e, "QOIMA_UI_DIR")
        return cls(
            bind_host=e.get("QOIMA_BIND_HOST", cls.bind_host),
            port=int(e.get("QOIMA_PORT", str(cls.port))),
            log_level=e.get("QOIMA_LOG_LEVEL", cls.log_level),
            config_dir=Path(config_dir) if config_dir else None,
            tls_cert_file=Path(cert) if cert else None,
            tls_key_file=Path(key) if key else None,
            ui_dir=Path(ui_dir) if ui_dir else None,
            run_migrations=e.get("QOIMA_RUN_MIGRATIONS", "1") not in {"0", "false", "no"},
            qdrant_url=_optional(e, "QOIMA_QDRANT_URL"),
            mediamtx_api_url=_optional(e, "QOIMA_MEDIAMTX_API_URL"),
            triton_url=_optional(e, "QOIMA_TRITON_URL"),
            service_urls=_parse_service_urls(_optional(e, "QOIMA_SERVICE_URLS")),
        )
