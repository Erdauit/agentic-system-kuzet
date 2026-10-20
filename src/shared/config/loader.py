"""Загрузка и валидация конфигов.

Порядок: configs/{defaults,alerts,camera_url_templates}.yaml + configs/scenarios/*.yaml,
затем поверх — переопределения объекта из site.yaml (глубокое слияние словарей).
Ошибка валидации превращается в список понятных сообщений с указанием файла и ключа.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from shared.config.models import QoimaConfig

ENV_CONFIG_DIR = "QOIMA_CONFIG_DIR"
ENV_SITE_FILE = "QOIMA_SITE_CONFIG"
ENV_HOME = "QOIMA_HOME"

DEFAULT_CONTAINER_CONFIG_DIR = Path("/etc/qoima/configs")


class ConfigError(Exception):
    """Конфиг не загрузился. errors — сообщения для человека, по одному на проблему."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def resolve_config_dir(explicit: Path | None = None) -> Path:
    """Каталог конфигов: аргумент → $QOIMA_CONFIG_DIR → $QOIMA_HOME/configs → ./configs →
    /etc/qoima/configs (путь внутри контейнеров)."""
    if explicit is not None:
        return explicit
    if env := os.environ.get(ENV_CONFIG_DIR):
        return Path(env)
    if home := os.environ.get(ENV_HOME):
        return Path(home) / "configs"
    local = Path.cwd() / "configs"
    if (local / "defaults.yaml").is_file():
        return local
    return DEFAULT_CONTAINER_CONFIG_DIR


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError([f"{path}: файл не найден"]) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path}: некорректный YAML: {exc}"]) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError([f"{path}: ожидается словарь на верхнем уровне"])
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Словари сливаются рекурсивно; списки и скаляры из override заменяют значение целиком."""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def _format_errors(exc: ValidationError, sources: Mapping[str, Path]) -> list[str]:
    messages: list[str] = []
    for err in exc.errors(include_url=False):
        loc = [str(p) for p in err["loc"]]
        source = sources.get(loc[0]) if loc else None
        # Для сценариев первым элементом идёт имя сценария — указываем его файл.
        if loc and loc[0] == "scenarios" and len(loc) > 1:
            source = sources.get(f"scenarios/{loc[1]}", source)
        where = ".".join(loc[1:]) if source is not None else ".".join(loc)
        prefix = f"{source}: " if source is not None else ""
        messages.append(f"{prefix}{where or '<корень>'}: {err['msg']}")
    return messages


def load_raw(
    config_dir: Path, site_file: Path | None = None
) -> tuple[dict[str, Any], dict[str, Path]]:
    sources: dict[str, Path] = {
        "defaults": config_dir / "defaults.yaml",
        "alerts": config_dir / "alerts.yaml",
        "camera_url_templates": config_dir / "camera_url_templates.yaml",
    }
    raw: dict[str, Any] = {key: _read_yaml(path) for key, path in sources.items()}

    scenarios: dict[str, Any] = {}
    scenario_dir = config_dir / "scenarios"
    for path in sorted(scenario_dir.glob("*.yaml")):
        scenarios[path.stem] = _read_yaml(path)
        sources[f"scenarios/{path.stem}"] = path
    raw["scenarios"] = scenarios

    # site.yaml создаёт мастер настройки; до первого сохранения файла может не быть —
    # тогда переопределений нет. Приоритет: аргумент → $QOIMA_SITE_CONFIG → configs/site.yaml.
    site_path: Path | None = None
    if site_file is not None:
        site_path = site_file if site_file.is_file() else None
    elif env := os.environ.get(ENV_SITE_FILE):
        site_path = Path(env) if Path(env).is_file() else None
    elif (config_dir / "site.yaml").is_file():
        site_path = config_dir / "site.yaml"
    if site_path is not None:
        site = _read_yaml(site_path)
        unknown = set(site) - set(raw)
        if unknown:
            raise ConfigError(
                [f"{site_path}: неизвестные разделы {sorted(unknown)}; допустимы {sorted(raw)}"]
            )
        raw = deep_merge(raw, site)
        sources["site"] = site_path
    return raw, sources


def load_config(config_dir: Path | None = None, site_file: Path | None = None) -> QoimaConfig:
    directory = resolve_config_dir(config_dir)
    raw, sources = load_raw(directory, site_file)
    try:
        return QoimaConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, sources)) from exc
