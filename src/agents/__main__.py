"""Точка входа agents.

Фаза 0: служебный HTTP и проверка зависимостей (Postgres — чекпоинты графов, Valkey — очереди).
Графы и плейбуки появятся в Фазах 4–8 (docs/TODO.md), до этого capabilities пуст.
"""

from __future__ import annotations

from agents import __version__
from shared.telemetry.checks import postgres_check, valkey_check
from shared.telemetry.runner import run


def main() -> None:
    run(
        service="agents",
        version=__version__,
        checks={"postgres": postgres_check(), "valkey": valkey_check()},
        capabilities=[],
        notes=[
            "VLM-арбитр — Фаза 4",
            "Chat Agent, Setup Assistant, Ops Agent — Фаза 5",
            "AutoML-граф — Фаза 7",
        ],
    )


if __name__ == "__main__":
    main()
