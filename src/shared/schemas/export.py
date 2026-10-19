"""Экспорт JSON Schema всех контрактов: `python -m shared.schemas.export <каталог>`.

Снапшоты лежат в tests/contract/snapshots/ и сравниваются в контрактных тестах: любое изменение
контракта видно в diff и требует осознанного обновления (`make schemas`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from shared.schemas import CONTRACTS


def schema_for(name: str) -> dict[str, Any]:
    return CONTRACTS[name].model_json_schema(mode="validation")


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(CONTRACTS):
        path = target / f"{name}.schema.json"
        path.write_text(render(schema_for(name)), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("использование: python -m shared.schemas.export <каталог>", file=sys.stderr)
        return 2
    for path in export(Path(args[0])):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
