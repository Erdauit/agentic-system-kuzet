"""Приёмка Фазы 3 на запущенной системе без GPU (SPEC.md §12): бенчмарк → план мощности.

    qoima up --no-gpu --build && qoima sim up --cameras 4 --build
    QOIMA_E2E_URL=https://localhost:8443 uv run pytest tests/e2e -m e2e

- `qoima bench` (короткий прогон) пишет BenchResult по реальным камерам-симуляторам;
- план воспроизводим: повторный расчёт по тем же входам — тот же план;
- PDF-отчёт скачивается; план применяется; шаг 2 мастера даёт вердикт по железу;
- включили сценарий на камере → «план устарел» (если модели установлены; без детектора
  сценарий не работает и нагрузку не меняет — план остаётся актуальным); вернули как было →
  план снова актуален.

Понижение FPS при камерах сверх ёмкости требует настоящих моделей — проверяется
интеграционным тестом tests/integration/test_capacity.py и на сервере с GPU (docs/TODO.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import httpx
import pytest

from tests.e2e.conftest import ROOT

pytestmark = pytest.mark.e2e


def qoima() -> str:
    cli = ROOT / ".venv-cli" / "bin" / "qoima"
    found = str(cli) if cli.exists() else shutil.which("qoima")
    if not found:
        pytest.skip("CLI qoima не установлен (install.sh)")
    return found


def test_bench_plan_apply_and_drift(api: httpx.Client) -> None:
    cams = [c for c in api.get("/api/v1/cameras").json() if c["camera"]["enabled"]]
    if not cams:
        pytest.skip("нет подключённых камер (сначала приёмка Фазы 1)")
    env = {**os.environ, "QOIMA_DATA_ROOT": os.environ.get("QOIMA_DATA_ROOT", str(ROOT / ".data"))}
    run = subprocess.run(
        [qoima(), "bench", "--no-gpu", "--quick"],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    bench = api.get("/api/v1/capacity/bench").json()
    assert bench, "бенчмарк не виден core-api"
    latest = bench[0]
    full = api.get(f"/api/v1/capacity/bench/{latest['id']}").json()
    assert any(d["source"] == "camera" for d in full["decode"]), full["warnings"]

    r1 = api.post("/api/v1/capacity/plans", json={"bench_id": latest["id"]})
    assert r1.status_code == 201, r1.text
    plan = r1.json()["plan"]
    assert plan["gpus"] == [] and plan["verdict"] in ("fits_with_limitations", "does_not_fit")
    r2 = api.post("/api/v1/capacity/plans", json={"bench_id": latest["id"]})
    assert r2.json()["plan"]["id"] == plan["id"]  # воспроизводимость: те же входы — тот же план
    assert r2.json()["plan"]["inputs_digest"] == plan["inputs_digest"]

    pdf = api.get(f"/api/v1/capacity/plans/{plan['id']}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    if plan["verdict"] != "does_not_fit":
        applied = api.post(f"/api/v1/capacity/plans/{plan['id']}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "active"
        st = api.get("/api/v1/capacity/status").json()
        assert st["active"]["plan"]["id"] == plan["id"] and st["stale"] is False

        cam = cams[0]["camera"]
        before = cam["scenarios"]
        toggled = (
            [s for s in before if s != "red_zone"]
            if "red_zone" in before
            else [*before, "red_zone"]
        )
        assert (
            api.patch(f"/api/v1/cameras/{cam['id']}", json={"scenarios": toggled}).status_code
            == 200
        )
        try:
            st = api.get("/api/v1/capacity/status").json()
            if plan["allocations"]:  # модели установлены — сценарий меняет нагрузку
                assert st["stale"] is True and any("изменились" in m for m in st["messages"])
            else:  # без детектора сценарий не работает и нагрузку не меняет — план актуален
                assert st["stale"] is False
        finally:
            api.patch(f"/api/v1/cameras/{cam['id']}", json={"scenarios": before})
        assert api.get("/api/v1/capacity/status").json()["stale"] is False

    hw = api.get("/api/v1/setup/hardware").json()
    assert (
        hw["bench_id"] == latest["id"] and hw["verdict"] == "does_not_fit"
    )  # без GPU — не для объекта
    assert any(i["name"] == "GPU" and i["level"] == "fail" for i in hw["items"])
