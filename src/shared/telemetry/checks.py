"""Типовые проверки готовности для /readyz фоновых сервисов."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from shared.telemetry.service import CheckResult, ReadinessCheck


def valkey_check() -> ReadinessCheck:
    """PING в Valkey. Клиент создаётся при первой проверке (нужен extra qoima-shared[valkey])."""
    state: dict[str, Any] = {}

    async def check() -> CheckResult:
        from shared.storage import valkey

        client = state.get("client")
        if client is None:
            client = state["client"] = valkey.create_client()
        latency = await valkey.ping(client)
        return CheckResult(True, "Valkey доступен", latency)

    return check


def postgres_check() -> ReadinessCheck:
    """SELECT 1 в Postgres (нужен extra qoima-shared[postgres])."""
    state: dict[str, Any] = {}

    async def check() -> CheckResult:
        from shared.storage import postgres

        engine = state.get("engine")
        if engine is None:
            engine = state["engine"] = postgres.create_engine()
        latency = await postgres.ping(engine)
        return CheckResult(True, "Postgres доступен", latency)

    return check


def gpu_check() -> ReadinessCheck:
    """GPU видны внутри контейнера: проверяет связку драйвер + nvidia-container-toolkit."""

    async def check() -> CheckResult:
        exe = shutil.which("nvidia-smi")
        if exe is None:
            return CheckResult(
                False,
                "nvidia-smi недоступен в контейнере: "
                "проверьте nvidia-container-toolkit и GPU-профиль",
            )
        proc = await asyncio.create_subprocess_exec(
            exe,
            "--query-gpu=index,name,memory.total",
            "--format=csv,noheader",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return CheckResult(
                False, f"nvidia-smi завершился с ошибкой: {err.decode(errors='replace').strip()}"
            )
        gpus = [line.strip() for line in out.decode().splitlines() if line.strip()]
        if not gpus:
            return CheckResult(False, "GPU не найдены")
        return CheckResult(True, "; ".join(gpus))

    return check
