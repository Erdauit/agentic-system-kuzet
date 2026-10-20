"""Общие помощники e2e: вход администратора, ожидание условия, неисправности симулятора."""

from __future__ import annotations

import os
import ssl
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("QOIMA_E2E_URL", "")
SECRETS = Path(os.environ.get("QOIMA_SECRETS_DIR", ROOT / ".secrets"))
SIM_CREDS = {"username": "admin", "password": "qoima-sim"}
NEW_PASSWORD = os.environ.get("QOIMA_E2E_ADMIN_PASSWORD", "e2e-admin-password-1")
CAMERAS = int(os.environ.get("QOIMA_E2E_CAMERAS", "4"))
OFFLINE_SLO_S = 15.0


def wait_for(predicate: Any, timeout_s: float, interval_s: float = 1.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"не дождались: {what} (последнее значение: {last!r})")


def sim_fault(camera: int, fault: str) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            f"qoima-camera-sim-{camera}",
            "sh",
            "-c",
            f"echo {fault} > /run/sim/fault",
        ],
        check=True,
    )


def tls() -> ssl.SSLContext:
    # Самоподписанный сертификат core-api проверяется по .secrets/tls.crt (SAN: localhost).
    return ssl.create_default_context(cafile=str(SECRETS / "tls.crt"))


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    if not URL:
        pytest.skip("QOIMA_E2E_URL не задан — e2e выполняется на запущенной системе")
    client = httpx.Client(base_url=URL, verify=tls(), timeout=60, headers={"x-qoima-request": "1"})
    initial = (SECRETS / "admin_initial_password").read_text().strip()
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": NEW_PASSWORD})
    if r.status_code != 200:
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": initial})
        assert r.status_code == 200, r.text
        assert r.json()["must_change_password"] is True
        # До смены пароля остальные разделы закрыты.
        assert client.get("/api/v1/cameras").status_code == 403
        r = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": initial, "new_password": NEW_PASSWORD},
        )
        assert r.status_code == 200, r.text
    yield client
    for n in range(1, CAMERAS + 1):
        subprocess.run(
            ["docker", "exec", f"qoima-camera-sim-{n}", "sh", "-c", "echo none > /run/sim/fault"],
            check=False,
        )
    client.close()
