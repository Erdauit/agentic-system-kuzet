"""Служебный HTTP-эндпоинт фоновых сервисов: /healthz, /readyz, /metrics.

Используется сервисами без собственного веб-фреймворка (vision-pipeline, agents, worker-gpu).
Это намеренно минимальный HTTP/1.0-сервер на asyncio: три GET-маршрута для Docker healthcheck,
core-api и Prometheus. Никакой бизнес-логики здесь нет.

/readyz честно сообщает, что сервис умеет: поле capabilities перечисляет реализованные функции.
Пустой список означает «процесс жив, но функциональность ещё не реализована» — core-api
показывает такой сервис жёлтым, а не зелёным (SPEC.md §2.10).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from shared.telemetry.logging import get_logger


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str
    latency_ms: float | None = None


ReadinessCheck = Callable[[], Awaitable[CheckResult]]


async def run_check(check: ReadinessCheck, timeout_s: float) -> CheckResult:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(check(), timeout=timeout_s)
    except TimeoutError:
        return CheckResult(False, f"таймаут {timeout_s} с", (time.perf_counter() - started) * 1000)
    except Exception as exc:  # проверка не должна ронять сервис
        return CheckResult(
            False, f"{type(exc).__name__}: {exc}", (time.perf_counter() - started) * 1000
        )
    if result.latency_ms is None:
        return CheckResult(result.ok, result.message, (time.perf_counter() - started) * 1000)
    return result


@dataclass
class ServiceRuntime:
    service: str
    version: str
    registry: CollectorRegistry
    checks: dict[str, ReadinessCheck] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    check_timeout_s: float = 2.0
    _server: asyncio.Server | None = field(default=None, init=False, repr=False)

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        names = list(self.checks)
        results = await asyncio.gather(
            *(run_check(self.checks[n], self.check_timeout_s) for n in names)
        )
        checks = {
            n: {"ok": r.ok, "message": r.message, "latency_ms": r.latency_ms}
            for n, r in zip(names, results, strict=True)
        }
        ready = all(r.ok for r in results)
        body = {
            "service": self.service,
            "version": self.version,
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "capabilities": self.capabilities,
            "notes": self.notes,
        }
        return ready, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log = get_logger("service-runtime")
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            # Остаток заголовков нам не нужен, но его надо дочитать до пустой строки.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            method, path = (parts[0], parts[1].split("?", 1)[0]) if len(parts) >= 2 else ("", "")
            if method != "GET":
                await self._respond(
                    writer, 405, "application/json", b'{"error":"method not allowed"}'
                )
            elif path == "/healthz":
                payload = {"service": self.service, "status": "alive"}
                await self._respond(writer, 200, "application/json", json.dumps(payload).encode())
            elif path == "/readyz":
                ready, body = await self.readiness()
                data = json.dumps(body, ensure_ascii=False).encode()
                await self._respond(
                    writer, 200 if ready else 503, "application/json; charset=utf-8", data
                )
            elif path == "/metrics":
                await self._respond(
                    writer, 200, CONTENT_TYPE_LATEST, generate_latest(self.registry)
                )
            else:
                await self._respond(writer, 404, "application/json", b'{"error":"not found"}')
        except (TimeoutError, ConnectionError) as exc:
            log.debug("service_http_client_error", error=str(exc))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes
    ) -> None:
        reason = {
            200: "OK",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }
        head = (
            f"HTTP/1.0 {status} {reason.get(status, 'OK')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        sockets = self._server.sockets
        return int(sockets[0].getsockname()[1]) if sockets else port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
