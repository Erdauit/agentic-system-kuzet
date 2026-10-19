"""Служебный HTTP фоновых сервисов: /healthz, /readyz (честные capabilities), /metrics."""

from __future__ import annotations

import asyncio
import json

from prometheus_client import CollectorRegistry

from shared.telemetry.metrics import build_info
from shared.telemetry.service import CheckResult, ServiceRuntime


async def _get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


async def test_endpoints() -> None:
    registry = CollectorRegistry()
    build_info("svc", "0.1.0", registry)

    async def ok() -> CheckResult:
        return CheckResult(True, "ok")

    async def broken() -> CheckResult:
        raise ConnectionError("нет связи")

    runtime = ServiceRuntime(
        "svc", "0.1.0", registry, checks={"ok": ok}, capabilities=[], notes=["Фаза 2"]
    )
    port = await runtime.start("127.0.0.1", 0)
    try:
        status, body = await _get(port, "/healthz")
        assert status == 200 and json.loads(body)["status"] == "alive"

        status, body = await _get(port, "/readyz")
        payload = json.loads(body)
        assert status == 200
        assert payload["capabilities"] == [] and payload["notes"] == ["Фаза 2"]

        status, body = await _get(port, "/metrics")
        assert status == 200 and b"qoima_build_info" in body

        status, _ = await _get(port, "/nope")
        assert status == 404

        runtime.checks["broken"] = broken
        status, body = await _get(port, "/readyz")
        payload = json.loads(body)
        assert status == 503
        assert payload["checks"]["broken"]["ok"] is False
        assert "нет связи" in payload["checks"]["broken"]["message"]
    finally:
        await runtime.stop()


async def test_check_timeout() -> None:
    async def slow() -> CheckResult:
        await asyncio.sleep(5)
        return CheckResult(True, "never")

    runtime = ServiceRuntime(
        "svc", "0", CollectorRegistry(), checks={"slow": slow}, check_timeout_s=0.05
    )
    ready, body = await runtime.readiness()
    assert not ready
    assert "таймаут" in body["checks"]["slow"]["message"]
