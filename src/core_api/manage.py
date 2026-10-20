"""Служебные команды внутри контейнера core-api (вызывает CLI `qoima user …` через docker exec).

python -m core_api.manage reset-password <username>
"""

from __future__ import annotations

import asyncio
import sys

from core_api.auth.passwords import generate_password
from core_api.auth.service import AuthError, AuthService
from shared.config import load_config
from shared.storage import postgres


async def reset_password(username: str) -> int:
    cfg = load_config()
    engine = postgres.create_engine()
    try:
        password = generate_password()
        await AuthService(engine, cfg.defaults.auth).set_temporary_password(
            username, password, "system:cli"
        )
    except AuthError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(password)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    match args:
        case ["reset-password", username]:
            return asyncio.run(reset_password(username))
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
