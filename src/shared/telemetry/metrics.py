"""Каталог обязательных метрик Prometheus (SPEC.md §6.2).

Каталог — единый источник имён, типов и меток: сервисы создают метрики только через него,
поэтому имена не расходятся между пайплайном, core-api и дашбордом. Метрика регистрируется
в процессе только тогда, когда сервис реально её измеряет — пустые «нулевые» метрики
не публикуются (SPEC.md §2.10).

gpu_util, gpu_mem_used, gpu_temp поставляет dcgm-exporter; одноимённые ряды строятся
recording-правилами Prometheus (configs/prometheus/rules.gpu.yml).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

MetricKind = Literal["counter", "gauge", "histogram"]

# Бакеты задержек: от 5 мс (одна стадия инференса) до 10 с (худший путь до алерта),
# с плотностью вокруг SLO 1–3 с (SPEC.md §6.1).
LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
)
# Батчи степеней двойки — так их собирают TensorRT-профили.
BATCH_BUCKETS: tuple[float, ...] = (1, 2, 4, 8, 16, 32, 64)
# Время подтверждения алерта оператором: от секунд до часа (эскалация на минутах).
ACK_BUCKETS: tuple[float, ...] = (5, 15, 30, 60, 120, 300, 600, 1800, 3600)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    kind: MetricKind
    help: str
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] | None = None
    source: Literal["qoima", "dcgm"] = "qoima"


_SPECS: tuple[MetricSpec, ...] = (
    # --- камеры
    MetricSpec("camera_fps_in", "gauge", "FPS входного потока камеры", ("camera",)),
    MetricSpec(
        "camera_fps_processed", "gauge", "FPS, реально обработанный пайплайном", ("camera",)
    ),
    MetricSpec(
        "camera_reconnects_total", "counter", "Переподключения к потоку камеры", ("camera",)
    ),
    MetricSpec("camera_frozen", "gauge", "1 — кадр камеры замёрз", ("camera",)),
    MetricSpec("camera_sharpness", "gauge", "Резкость относительно базовой линии", ("camera",)),
    MetricSpec(
        "camera_view_shift_score",
        "gauge",
        "Сдвиг ракурса относительно калибровки (0..1)",
        ("camera",),
    ),
    # --- пайплайн
    MetricSpec("decode_errors_total", "counter", "Ошибки декодирования", ("camera",)),
    MetricSpec(
        "pipeline_latency_seconds",
        "histogram",
        "Задержка пайплайна по стадиям",
        ("stage",),
        LATENCY_BUCKETS,
    ),
    MetricSpec(
        "inference_latency_seconds",
        "histogram",
        "Задержка инференса модели",
        ("model",),
        LATENCY_BUCKETS,
    ),
    MetricSpec("batch_size", "histogram", "Размер батча инференса", ("model",), BATCH_BUCKETS),
    # --- GPU (через DCGM)
    MetricSpec("gpu_util", "gauge", "Загрузка GPU, %", ("gpu",), source="dcgm"),
    MetricSpec("gpu_mem_used", "gauge", "Занятая видеопамять, МБ", ("gpu",), source="dcgm"),
    MetricSpec("gpu_temp", "gauge", "Температура GPU, °C", ("gpu",), source="dcgm"),
    # --- VLM
    MetricSpec("vlm_requests_total", "counter", "Запросы к VLM по приоритету", ("priority",)),
    MetricSpec(
        "vlm_latency_seconds", "histogram", "Задержка ответа VLM", ("priority",), LATENCY_BUCKETS
    ),
    MetricSpec("vlm_queue_depth", "gauge", "Глубина очереди запросов к VLM", ("priority",)),
    MetricSpec("vlm_timeouts_total", "counter", "Таймауты VLM", ("priority",)),
    MetricSpec(
        "vlm_failopen_total", "counter", "Алерты, ушедшие без арбитража (fail-open)", ("reason",)
    ),
    # --- события и алерты
    MetricSpec("events_total", "counter", "События по типу и камере", ("type", "camera")),
    MetricSpec("alerts_total", "counter", "Алерты по важности", ("severity",)),
    MetricSpec(
        "alert_ack_seconds",
        "histogram",
        "Время до подтверждения алерта",
        ("severity",),
        ACK_BUCKETS,
    ),
    MetricSpec("alert_escalations_total", "counter", "Эскалации алертов", ("level",)),
    MetricSpec(
        "false_alarms_per_camera_shift",
        "gauge",
        "Ложные срабатывания за смену (по фидбеку)",
        ("camera", "shift"),
    ),
    # --- инфраструктура
    MetricSpec("disk_free_bytes", "gauge", "Свободное место на томе", ("volume",)),
    MetricSpec("gop_buffer_bytes", "gauge", "Объём GOP-буфера в RAM", ("camera",)),
    MetricSpec(
        "canary_e2e_seconds", "histogram", "Сквозная задержка канарейки", (), LATENCY_BUCKETS
    ),
    MetricSpec("canary_failures_total", "counter", "Провалы канарейки", ()),
    # --- SLO (SPEC.md §6.1): сверх обязательного списка §6.2
    MetricSpec(
        "alert_latency_seconds",
        "histogram",
        "От срабатывания FSM до отправки первого алерта, по типу события",
        ("type",),
        LATENCY_BUCKETS,
    ),
    MetricSpec(
        "camera_offline_detection_seconds",
        "histogram",
        "От последнего пакета камеры до системного алерта «нет потока» (SLO ≤ 15 с)",
        (),
        (1, 2, 5, 8, 10, 12, 15, 20, 30, 60),
    ),
)

CATALOG: dict[str, MetricSpec] = {spec.name: spec for spec in _SPECS}

Metric = Counter | Gauge | Histogram


def create_metric(name: str, registry: CollectorRegistry) -> Metric:
    """Создаёт метрику из каталога в указанном реестре."""
    spec = CATALOG.get(name)
    if spec is None:
        raise KeyError(f"метрики {name} нет в каталоге shared.telemetry.metrics.CATALOG")
    if spec.source != "qoima":
        raise ValueError(f"метрику {name} публикует {spec.source}, а не сервисы Qoima")
    labels = list(spec.labels)
    if spec.kind == "counter":
        # prometheus_client сам добавляет суффикс _total к счётчикам.
        base = name.removesuffix("_total")
        return Counter(base, spec.help, labels, registry=registry)
    if spec.kind == "gauge":
        return Gauge(name, spec.help, labels, registry=registry)
    return Histogram(
        name, spec.help, labels, registry=registry, buckets=spec.buckets or LATENCY_BUCKETS
    )


def build_info(service: str, version: str, registry: CollectorRegistry) -> Gauge:
    """Стандартная метрика qoima_build_info{service,version} = 1."""
    g = Gauge("qoima_build_info", "Версия сервиса Qoima", ["service", "version"], registry=registry)
    g.labels(service=service, version=version).set(1)
    return g
