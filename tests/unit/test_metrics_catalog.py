"""Каталог метрик совпадает с обязательным списком SPEC.md §6.2."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from shared.telemetry.metrics import CATALOG, create_metric

SPEC_METRICS = {
    "camera_fps_in", "camera_fps_processed", "camera_reconnects_total", "camera_frozen",
    "camera_sharpness", "camera_view_shift_score",
    "decode_errors_total", "pipeline_latency_seconds", "inference_latency_seconds", "batch_size",
    "gpu_util", "gpu_mem_used", "gpu_temp",
    "vlm_requests_total", "vlm_latency_seconds", "vlm_queue_depth", "vlm_timeouts_total",
    "vlm_failopen_total",
    "events_total", "alerts_total", "alert_ack_seconds", "alert_escalations_total",
    "false_alarms_per_camera_shift",
    "disk_free_bytes", "gop_buffer_bytes", "canary_e2e_seconds", "canary_failures_total",
}  # fmt: skip


def test_catalog_covers_spec() -> None:
    assert set(CATALOG) >= SPEC_METRICS


@pytest.mark.parametrize("name", sorted(n for n, s in CATALOG.items() if s.source == "qoima"))
def test_metric_can_be_created_and_exported(name: str) -> None:
    registry = CollectorRegistry()
    metric = create_metric(name, registry)
    spec = CATALOG[name]
    labels = dict.fromkeys(spec.labels, "x")
    child = metric.labels(**labels) if labels else metric
    if spec.kind == "counter":
        child.inc()  # type: ignore[union-attr]
    elif spec.kind == "gauge":
        child.set(1)  # type: ignore[union-attr]
    else:
        child.observe(0.1)  # type: ignore[union-attr]
    assert name.encode() in generate_latest(registry)


def test_dcgm_metrics_not_created_by_services() -> None:
    with pytest.raises(ValueError, match="dcgm"):
        create_metric("gpu_util", CollectorRegistry())
