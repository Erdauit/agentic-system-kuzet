"""Структурированные JSON-логи (structlog) с маскированием секретов (SPEC.md §10).

Пароли, токены и учётные данные в URL (rtsp://user:pass@host) никогда не попадают в лог:
процессор redact маскирует их и в полях события, и внутри строк.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

MASK = "***"

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential)", re.IGNORECASE
)
# scheme://user:password@host → scheme://user:***@host
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>[a-z][a-z0-9+.\-]*://[^:/@\s]+:)(?P<secret>[^@/\s]+)@", re.IGNORECASE
)


def redact_text(text: str) -> str:
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group('prefix')}{MASK}@", text)


def redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, v) for v in value]
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


def _shared_processors(service: str) -> list[Any]:
    def add_service(
        _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        return event_dict

    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_service,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]


def configure_logging(service: str, level: str = "INFO", *, json_output: bool = True) -> None:
    """Настраивает structlog и стандартный logging (uvicorn, sqlalchemy) на единый формат.

    Логи пишутся в stdout; ротацию выполняет Docker (json-file, max-size/max-file в compose).
    """
    log_level = logging.getLevelNamesMapping().get(level.upper())
    if log_level is None:
        raise ValueError(f"неизвестный уровень логирования: {level}")

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    shared = _shared_processors(service)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(log_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # Каждый HTTP-запрос клиента httpx (health-проверки раз в 10 с) — шум; оставляем предупреждения.
    logging.getLogger("httpx").setLevel(max(log_level, logging.WARNING))


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
