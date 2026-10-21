"""Замер VLM на типовом запросе арбитра (SPEC.md §5.10, §9.1): 4 ROI-кропа + ответ строго по
JSON-схеме VLMVerdict. Сервер — любой OpenAI-совместимый (vLLM и SGLang оба им являются), поэтому
один и тот же замер выбирает между ними в Фазе 4.

Пропускная способность — запросов/с при заданном числе одновременных запросов; ответ, который не
проходит контракт VLMVerdict, считается невалидным (арбитр в этом случае делает fail-open).
"""

from __future__ import annotations

import asyncio
import base64
import statistics
import time
from typing import Any

import httpx
import numpy as np
from pydantic import ValidationError

from shared.config.models import BenchConfig
from shared.schemas import VlmMeasurement, VLMVerdict
from shared.telemetry import get_logger

log = get_logger("bench-vlm")

ARBITER_SYSTEM = (
    "Ты — арбитр системы промышленной безопасности. По фрагментам кадров камеры оцени, есть ли "
    "опасность для человека. Ответь строго JSON по схеме. Ты не можешь отменить тревогу — только "
    "предложить повысить или понизить её приоритет."
)
ARBITER_USER = (
    "Событие: падение человека (FallFSM, лежит 10 с). Камера «Линия упаковки». Фрагменты кадров "
    "от начала события до подтверждения, по порядку."
)


def arbiter_request(model: str, images: list[bytes], max_tokens: int = 400) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": ARBITER_USER}]
    for jpeg in images:
        b64 = base64.b64encode(jpeg).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": ARBITER_SYSTEM},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "vlm_verdict", "schema": VLMVerdict.model_json_schema()},
        },
    }


async def detect_server(http: httpx.AsyncClient, base: str) -> tuple[str, str]:
    """(сервер, модель) по /v1/models: vLLM и SGLang указывают себя в owned_by."""
    resp = await http.get(f"{base}/v1/models")
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        raise RuntimeError("сервер VLM не отдал ни одной модели (/v1/models пуст)")
    owner = str(data[0].get("owned_by") or "unknown").lower()
    server = "vllm" if "vllm" in owner else "sglang" if "sglang" in owner else "unknown"
    return server, str(data[0]["id"])


def _valid(body: dict[str, Any]) -> bool:
    try:
        text = body["choices"][0]["message"]["content"]
        VLMVerdict.model_validate_json(text)
        return True
    except (KeyError, IndexError, TypeError, ValidationError, ValueError):
        return False


async def _level(
    http: httpx.AsyncClient,
    base: str,
    payload: dict[str, Any],
    concurrency: int,
    n: int,
    request_timeout_s: float,
) -> tuple[float, list[float], int]:
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    valid = 0

    async def one() -> None:
        nonlocal valid
        async with sem:
            started = time.perf_counter()
            try:
                resp = await http.post(
                    f"{base}/v1/chat/completions", json=payload, timeout=request_timeout_s
                )
                ok = resp.status_code == 200 and _valid(resp.json())
            except (httpx.HTTPError, ValueError):
                ok = False
            latencies.append(time.perf_counter() - started)
            valid += int(ok)

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    return time.perf_counter() - started, latencies, valid


async def measure_vlm(
    base_url: str,
    model: str | None,
    images: list[bytes],
    cfg: BenchConfig,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[VlmMeasurement], list[str]]:
    base = base_url.rstrip("/")
    out: list[VlmMeasurement] = []
    async with httpx.AsyncClient(transport=transport) as http:
        try:
            server, default_model = await detect_server(http, base)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return [], [f"VLM {base} недоступен: {exc}"]
        name = model or default_model
        payload = arbiter_request(name, images)
        for c in cfg.vlm_concurrency:
            elapsed, lat, valid = await _level(
                http, base, payload, c, cfg.vlm_requests_per_level, cfg.vlm_timeout_s
            )
            n = len(lat)
            out.append(
                VlmMeasurement(
                    server=server,
                    model=name,
                    concurrency=c,
                    requests=n,
                    rps=round(valid / elapsed, 4) if elapsed > 0 else 0.0,
                    latency_p50_s=round(statistics.median(lat), 3),
                    latency_p95_s=round(float(np.percentile(lat, 95)), 3),
                    valid_json_ratio=round(valid / n, 4),
                )
            )
            log.info("vlm_bench", concurrency=c, rps=out[-1].rps, valid=out[-1].valid_json_ratio)
    return out, []
