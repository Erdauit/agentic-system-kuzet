"""Пути установки: QOIMA_HOME (корень репозитория/поставки), секреты, данные."""

from __future__ import annotations

import os
from pathlib import Path

REQUIRED_SECRETS = (
    "postgres_password",
    "valkey_password",
    "tls.crt",
    "tls.key",
    "admin_initial_password",
)
# Каталог секретов, которые вводят в мастере (пароли камер, токен бота); пишет core-api (uid 10001).
RUNTIME_SECRETS = "runtime"

# Подкаталоги данных и владельцы (uid:gid процессов в контейнерах). Создаёт install.sh.
DATA_DIRS: dict[str, tuple[int, int]] = {
    "clips": (10001, 10001),
    "frames": (10001, 10001),
    "datasets": (10001, 10001),
    "models": (10001, 10001),
    "models/triton": (10001, 10001),
    "config": (10001, 10001),
    "doctor": (10001, 10001),
    "postgres": (999, 999),
    "valkey": (999, 999),
    "qdrant": (0, 0),
    "prometheus": (65534, 65534),
    "grafana": (472, 0),
}


class HomeNotFoundError(RuntimeError):
    pass


def qoima_home() -> Path:
    """$QOIMA_HOME, иначе ближайший родительский каталог с compose/compose.yml."""
    if env := os.environ.get("QOIMA_HOME"):
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "compose" / "compose.yml").is_file():
            return candidate
    raise HomeNotFoundError(
        "Не найден каталог установки Qoima (нет compose/compose.yml). "
        "Запустите команду из каталога установки или задайте QOIMA_HOME."
    )


def secrets_dir(home: Path) -> Path:
    return Path(os.environ.get("QOIMA_SECRETS_DIR", str(home / ".secrets"))).resolve()


def data_root(default: Path) -> Path:
    """Каталог данных на хосте: $QOIMA_DATA_ROOT или storage.data_root из конфига."""
    return Path(os.environ.get("QOIMA_DATA_ROOT", str(default))).resolve()
