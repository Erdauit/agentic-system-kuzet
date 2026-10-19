"""Метрики Prometheus, структурированные логи и служебный HTTP-эндпоинт сервисов."""

from shared.telemetry.logging import configure_logging, get_logger, redact_text
from shared.telemetry.metrics import CATALOG, MetricSpec, build_info, create_metric
from shared.telemetry.service import CheckResult, ReadinessCheck, ServiceRuntime, run_check

__all__ = [
    "CATALOG",
    "CheckResult",
    "MetricSpec",
    "ReadinessCheck",
    "ServiceRuntime",
    "build_info",
    "configure_logging",
    "create_metric",
    "get_logger",
    "redact_text",
    "run_check",
]
