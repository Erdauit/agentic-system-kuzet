"""Чтение секретов из файлов (Docker secrets / .secrets/). Значения никогда не логируются."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr

DEFAULT_SECRETS_DIR = Path("/run/secrets")
ENV_SECRETS_DIR = "QOIMA_SECRETS_DIR"


class SecretNotFoundError(FileNotFoundError):
    pass


def secrets_dir() -> Path:
    return Path(os.environ.get(ENV_SECRETS_DIR, str(DEFAULT_SECRETS_DIR)))


def read_secret(name: str, *, path_env: str | None = None) -> SecretStr:
    """Секрет из файла: путь из переменной path_env (если задана) или <secrets_dir>/<name>.

    Возвращает SecretStr: при печати и в repr значение замаскировано.
    """
    path = (
        Path(os.environ[path_env])
        if path_env and os.environ.get(path_env)
        else secrets_dir() / name
    )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SecretNotFoundError(f"секрет {name} не найден: {path}") from exc
    if not value:
        raise SecretNotFoundError(f"секрет {name} пуст: {path}")
    return SecretStr(value)
