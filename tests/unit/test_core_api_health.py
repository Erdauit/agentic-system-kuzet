"""core-api: сбор «Состояния системы» и маршруты (без реальных Postgres/Valkey)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core_api.app import create_app
from core_api.settings import Settings, _parse_service_urls
from core_api.system_health import (
    CHECKS,
    ComponentCheck,
    HealthDeps,
    Probe,
    collect,
    service_check,
)
from shared.config import QoimaConfig
from shared.schemas import ComponentKind, HealthStatus


def _deps(config: QoimaConfig, transport: httpx.MockTransport, **settings: object) -> HealthDeps:
    return HealthDeps(
        config=config,
        settings=Settings(**settings),  # type: ignore[arg-type]
        engine=None,
        valkey=None,
        http=httpx.AsyncClient(transport=transport),
        alembic_head="0001",
    )


def _readyz(capabilities: list[str], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "service": "vision-pipeline",
            "version": "0.1.0",
            "status": "ready" if status == 200 else "not_ready",
            "checks": {"valkey": {"ok": status == 200, "message": "x"}},
            "capabilities": capabilities,
            "notes": ["Фаза 2"],
        }
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("capabilities", "status", "expected"),
    [
        ([], 200, HealthStatus.YELLOW),  # процесс жив, но работы не делает — не зелёный
        (["camera_health"], 200, HealthStatus.GREEN),
        ([], 503, HealthStatus.RED),
    ],
)
async def test_service_check_is_honest(
    config: QoimaConfig, capabilities: list[str], status: int, expected: HealthStatus
) -> None:
    deps = _deps(
        config, _readyz(capabilities, status), service_urls={"vision-pipeline": "http://vp:8080"}
    )
    probe = await service_check("vision-pipeline")(deps)
    assert probe.status is expected


async def test_not_configured_service_is_not_deployed(config: QoimaConfig) -> None:
    deps = _deps(config, _readyz([]))
    probe = await service_check("agents")(deps)
    assert probe.status is HealthStatus.NOT_DEPLOYED


async def test_collect_survives_failing_and_hanging_checks(config: QoimaConfig) -> None:
    async def boom(_: HealthDeps) -> Probe:
        raise RuntimeError("сломалось")

    async def hang(_: HealthDeps) -> Probe:
        await asyncio.sleep(10)
        return Probe(HealthStatus.GREEN, "never")

    async def fine(_: HealthDeps) -> Probe:
        return Probe(HealthStatus.GREEN, "ok")

    cfg = config.model_copy(
        update={
            "defaults": config.defaults.model_copy(
                update={
                    "health": config.defaults.health.model_copy(
                        update={"component_timeout_s": 0.05}
                    )
                }
            )
        }
    )
    checks = (
        ComponentCheck("a", ComponentKind.DATABASE, boom),
        ComponentCheck("b", ComponentKind.QUEUES, hang),
        ComponentCheck("c", ComponentKind.DISK, fine),
    )
    health = await collect(_deps(cfg, _readyz([])), checks)
    by_name = {c.component: c for c in health.components}
    assert by_name["a"].status is HealthStatus.RED and "сломалось" in by_name["a"].message
    assert by_name["b"].status is HealthStatus.RED and "не уложилась" in by_name["b"].message
    assert by_name["c"].status is HealthStatus.GREEN
    assert health.status is HealthStatus.RED


async def test_full_collect_without_infrastructure(config: QoimaConfig, tmp_path: Path) -> None:
    """Без БД и Valkey страница всё равно строится: инфраструктура красная, остальное честно."""
    cfg = config.model_copy(
        update={
            "defaults": config.defaults.model_copy(
                update={
                    "storage": config.defaults.storage.model_copy(update={"data_root": tmp_path})
                }
            )
        }
    )
    health = await collect(_deps(cfg, _readyz([])))
    by_name = {c.component: c.status for c in health.components}
    assert len(health.components) == len(CHECKS)
    assert by_name["postgres"] is HealthStatus.RED
    assert by_name["valkey"] is HealthStatus.RED
    assert by_name["llm"] is HealthStatus.NOT_DEPLOYED
    assert by_name["triton"] is HealthStatus.NOT_DEPLOYED
    assert by_name["disk"] in (HealthStatus.GREEN, HealthStatus.YELLOW, HealthStatus.RED)
    assert by_name["cameras"] is HealthStatus.UNKNOWN


def test_routes_and_spa(config: QoimaConfig, tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html>qoima</html>")
    (ui / "assets" / "app.js").write_text("console.log(1)")

    async def fine(_: HealthDeps) -> Probe:
        return Probe(HealthStatus.GREEN, "ok")

    deps = _deps(config, _readyz([]))
    app = create_app(Settings(ui_dir=ui), config=config, health_deps=deps)
    with TestClient(app) as client:
        assert client.get("/api/v1/healthz").json()["status"] == "alive"
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        body = health.json()
        assert {c["component"] for c in body["components"]} == {c.name for c in CHECKS}
        metrics = client.get("/metrics", follow_redirects=False)  # ровно так скрейпит Prometheus
        assert metrics.status_code == 200
        assert "qoima_build_info" in metrics.text
        assert 'disk_free_bytes{volume="data"}' in metrics.text
        assert client.get("/").text == "<html>qoima</html>"
        assert client.get("/health").text == "<html>qoima</html>"  # прямая ссылка на раздел SPA
        assert client.get("/assets/app.js").text == "console.log(1)"
        assert client.get("/api/v1/unknown").status_code == 404
        assert client.get("/../../etc/passwd").text == "<html>qoima</html>"


def test_no_ui_build(config: QoimaConfig) -> None:
    app = create_app(Settings(ui_dir=None), config=config, health_deps=_deps(config, _readyz([])))
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 503
        assert "UI не собран" in json.loads(resp.text)["detail"]


def test_service_urls_parsing() -> None:
    assert _parse_service_urls("a=http://a:1/, b=http://b:2") == {
        "a": "http://a:1",
        "b": "http://b:2",
    }
    with pytest.raises(ValueError, match="имя=url"):
        _parse_service_urls("broken")


def test_tls_settings_must_be_paired() -> None:
    with pytest.raises(ValueError, match="вместе"):
        Settings.from_env({"QOIMA_TLS_CERT_FILE": "/x.crt"})
