"""Запуск миграций Alembic из кода и из командной строки.

    python -m core_api.db.migrate upgrade        # до head
    python -m core_api.db.migrate current
    python -m core_api.db.migrate downgrade <rev>

Новая миграция (разработчику, нужен запущенный Postgres):
    alembic -c src/core_api/db/alembic.ini revision --autogenerate -m "описание"
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        cfg.attributes["database_url"] = database_url
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("в каталоге миграций нет ни одной ревизии")
    return head


def upgrade_head(database_url: str | None = None) -> None:
    """Синхронный вызов: нельзя выполнять внутри работающего event loop (env.py сам запускает asyncio)."""
    command.upgrade(alembic_config(database_url), "head")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    cfg = alembic_config()
    match args:
        case ["upgrade"]:
            command.upgrade(cfg, "head")
        case ["upgrade", rev]:
            command.upgrade(cfg, rev)
        case ["downgrade", rev]:
            command.downgrade(cfg, rev)
        case ["current"]:
            command.current(cfg, verbose=True)
        case _:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
