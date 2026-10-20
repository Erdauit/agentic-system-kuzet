"""Приёмка Фазы 1 (SPEC.md §12) на запущенной системе и симуляторе камер.

    qoima up --no-gpu --build && qoima sim up --cameras 4 --build
    QOIMA_E2E_URL=https://localhost:8443 uv run pytest tests/e2e -m e2e

Сценарий: вход администратора → автопоиск находит 4 камеры (ONVIF) → «подключить всё» →
все камеры зелёные и видны вживую (WHEP) → остановленная камера красная и системный алерт
≤ 15 с → замёрзший кадр и расфокус обнаруживаются.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    CAMERAS,
    OFFLINE_SLO_S,
    ROOT,
    SIM_CREDS,
    URL,
    sim_fault,
    tls,
    wait_for,
)

pytestmark = pytest.mark.e2e


def test_csrf_header_required(api: httpx.Client) -> None:
    r = httpx.post(
        f"{URL}/api/v1/auth/login", json={"username": "x", "password": "y"}, verify=tls()
    )
    assert r.status_code == 403
    assert httpx.get(f"{URL}/api/v1/cameras", verify=tls()).status_code == 401


def _cameras(api: httpx.Client) -> list[dict[str, Any]]:
    r = api.get("/api/v1/cameras")
    assert r.status_code == 200, r.text
    return list(r.json())


def test_discovery_finds_and_adds_all_cameras(api: httpx.Client) -> None:
    if len(_cameras(api)) >= CAMERAS:
        pytest.skip("камеры уже подключены предыдущим прогоном")
    r = api.post("/api/v1/cameras/discover", json={"credentials": [SIM_CREDS]})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    job = wait_for(
        lambda: (
            (j := api.get(f"/api/v1/cameras/discover/{job_id}").json())["status"] != "running" and j
        ),
        180,
        2,
        "завершение автопоиска",
    )
    assert job["status"] == "done", job
    sims = [d for d in job["devices"] if d.get("manufacturer") == "Qoima Sim"]
    assert len(sims) >= CAMERAS, f"найдено {len(sims)} симуляторов: {job['devices']}"
    for d in sims:
        assert d["auth"] == "ok"
        assert {c["profile"] for c in d["candidates"]} == {"mainStream", "subStream"}

    r = api.post(
        f"/api/v1/cameras/discover/{job_id}/add", json={"hosts": [d["host"] for d in sims]}
    )
    assert r.status_code == 200, r.text
    results = r.json()
    assert all(x["ok"] for x in results), results
    cams = _cameras(api)
    assert len(cams) >= CAMERAS
    for c in cams:
        # Автоподбор: main — 1280×720, substream — 640×360, базовая линия снята.
        roles = {p["role"]: (p["width"], p["height"]) for p in c["probes"]}
        assert roles == {"main": (1280, 720), "sub": (640, 360)}, c
        assert c["baseline"] is not None
        assert c["has_credentials"] is True


def _status(api: httpx.Client, camera_id: str) -> dict[str, Any]:
    r = api.get(f"/api/v1/cameras/{camera_id}")
    assert r.status_code == 200
    return dict(r.json())


def _sim_index(camera: dict[str, Any]) -> int:
    """Номер контейнера симулятора по IP камеры."""
    host = camera["camera"]["main_url"].split("//", 1)[1].split(":", 1)[0]
    for n in range(1, CAMERAS + 1):
        out = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                f"qoima-camera-sim-{n}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if host in out:
            return n
    raise AssertionError(f"контейнер симулятора для {host} не найден")


def test_all_cameras_green_and_live(api: httpx.Client) -> None:
    cams = _cameras(api)[:CAMERAS]
    for c in cams:
        wait_for(
            lambda c=c: _status(api, c["camera"]["id"])["status"] == "ok",
            90,
            2,
            f"{c['camera']['id']} зелёная",
        )
    health = api.get("/api/v1/health").json()
    cameras = next(x for x in health["components"] if x["component"] == "cameras")
    assert cameras["status"] == "green", cameras
    pipeline = next(x for x in health["components"] if x["component"] == "vision-pipeline")
    assert pipeline["status"] == "green", pipeline
    # Live: сигнализация WHEP через core-api возвращает SDP-ответ MediaMTX.
    offer = (ROOT / "tests" / "e2e" / "whep_offer.sdp").read_text()
    r = api.post(
        f"/api/v1/cameras/{cams[0]['camera']['id']}/whep",
        content=offer,
        headers={"Content-Type": "application/sdp"},
    )
    assert r.status_code == 201, r.text
    assert "v=0" in r.text and r.headers["location"].startswith(
        f"/api/v1/cameras/{cams[0]['camera']['id']}/whep/"
    )
    api.delete(r.headers["location"])


def test_stopped_camera_red_with_system_alert_within_slo(api: httpx.Client) -> None:
    cam = _cameras(api)[0]
    camera_id = cam["camera"]["id"]
    n = _sim_index(cam)
    sim_fault(n, "stop")
    started = time.monotonic()
    try:
        wait_for(lambda: _status(api, camera_id)["status"] == "down", 30, 0.5, "камера красная")
        alerts = wait_for(
            lambda: [
                a
                for a in api.get("/api/v1/system-alerts").json()
                if a["kind"] == "camera_down" and a["camera_id"] == camera_id
            ],
            30,
            0.5,
            "системный алерт camera_down",
        )
        elapsed = time.monotonic() - started
        detection = alerts[0]["details"].get("detection_seconds")
        assert detection is not None and detection <= OFFLINE_SLO_S, alerts[0]
        assert elapsed <= OFFLINE_SLO_S + 3, (
            f"алерт через {elapsed:.1f} с (SLO {OFFLINE_SLO_S} с + опрос)"
        )
        health = api.get("/api/v1/health").json()
        assert (
            next(x for x in health["components"] if x["component"] == "cameras")["status"] == "red"
        )
    finally:
        sim_fault(n, "none")
    wait_for(lambda: _status(api, camera_id)["status"] == "ok", 90, 2, "камера снова зелёная")
    wait_for(
        lambda: (
            not [a for a in api.get("/api/v1/system-alerts").json() if a["camera_id"] == camera_id]
        ),
        60,
        2,
        "алерт снят",
    )


@pytest.mark.parametrize(
    ("fault", "issue", "kind"),
    [("freeze", "frozen", "camera_frozen"), ("blur", "blur", "camera_blur")],
)
def test_frozen_and_blur_detected(api: httpx.Client, fault: str, issue: str, kind: str) -> None:
    cam = _cameras(api)[1]
    camera_id = cam["camera"]["id"]
    n = _sim_index(cam)
    sim_fault(n, fault)
    try:
        status = wait_for(
            lambda: issue in (s := _status(api, camera_id))["health"]["issues"] and s,
            60,
            1,
            f"обнаружение {issue}",
        )
        assert status["status"] == "degraded"
        wait_for(
            lambda: [
                a
                for a in api.get("/api/v1/system-alerts").json()
                if a["kind"] == kind and a["camera_id"] == camera_id
            ],
            30,
            1,
            f"системный алерт {kind}",
        )
    finally:
        sim_fault(n, "none")
    wait_for(lambda: _status(api, camera_id)["status"] == "ok", 120, 2, "камера снова зелёная")
