"""Настройки объекта, которые вводят в мастере: файл site.yaml в каталоге данных.

configs/*.yaml — значения продукта (только чтение, приходят с поставкой). site.yaml — изменения
объекта поверх них (шаги 1 и 7 мастера). Перед записью полная конфигурация проверяется
QoimaConfig: невалидная настройка не попадёт на диск. Запись атомарна, изменение — в аудит.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from shared.config import ConfigError, QoimaConfig, deep_merge
from shared.config.loader import _format_errors, load_raw

HEADER = (
    "# Настройки объекта Qoima Vision. Файл ведёт мастер настройки (UI); ручные правки\n"
    "# допустимы, после них: qoima config validate. Значения по умолчанию — configs/defaults.yaml.\n"
)


class SiteConfigStore:
    def __init__(self, config_dir: Path, site_file: Path) -> None:
        self.config_dir = config_dir
        self.site_file = site_file

    def read(self) -> dict[str, Any]:
        if not self.site_file.is_file():
            return {}
        data = yaml.safe_load(self.site_file.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError([f"{self.site_file}: ожидается словарь"])
        return data

    def validate(self, site: dict[str, Any]) -> QoimaConfig:
        raw, sources = load_raw(self.config_dir, site_file=Path("/nonexistent-site.yaml"))
        unknown = set(site) - set(raw)
        if unknown:
            raise ConfigError([f"неизвестные разделы {sorted(unknown)}"])
        try:
            return QoimaConfig.model_validate(deep_merge(raw, site))
        except ValidationError as exc:
            raise ConfigError(_format_errors(exc, sources)) from exc

    def update(self, patch: dict[str, Any]) -> QoimaConfig:
        """Сливает patch с текущим site.yaml, проверяет и атомарно записывает."""
        merged = deep_merge(self.read(), patch)
        cfg = self.validate(merged)
        self.site_file.parent.mkdir(parents=True, exist_ok=True)
        text = HEADER + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
        fd, tmp = tempfile.mkstemp(dir=self.site_file.parent, prefix=".site-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            Path(tmp).replace(self.site_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return cfg
