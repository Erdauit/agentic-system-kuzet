"""Логи: JSON, кириллица без экранирования, секреты замаскированы (SPEC.md §10)."""

from __future__ import annotations

import json
import logging

import pytest

from shared.telemetry.logging import configure_logging, get_logger, redact_text, redact_value


def test_redact_url_credentials() -> None:
    assert (
        redact_text("rtsp://admin:S3cr3t@10.0.0.5:554/ch1") == "rtsp://admin:***@10.0.0.5:554/ch1"
    )
    assert redact_text("rtsp://10.0.0.5/ch1") == "rtsp://10.0.0.5/ch1"


def test_redact_sensitive_keys_nested() -> None:
    value = redact_value("payload", {"password": "x", "nested": {"api_key": "y", "ok": "z"}})
    assert value == {"password": "***", "nested": {"api_key": "***", "ok": "z"}}


def test_json_output_redacts_and_keeps_cyrillic(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("test-service", "INFO")
    get_logger("t").info("камера подключена", url="rtsp://u:p@host/x", token="abc")
    logging.getLogger("uvicorn.error").warning("сообщение uvicorn")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    first = lines[0]
    assert first["event"] == "камера подключена"
    assert first["url"] == "rtsp://u:***@host/x"
    assert first["token"] == "***"
    assert first["service"] == "test-service"
    assert lines[1]["event"] == "сообщение uvicorn"


def test_unknown_level_rejected() -> None:
    with pytest.raises(ValueError, match="уровень"):
        configure_logging("x", "LOUD")
