"""Приёмка Фазы 2 без моделей и тестовых роликов (SPEC.md §12): путь событие → инцидент → алерт.

    qoima up --no-gpu --build && qoima sim up --cameras 4 --build
    QOIMA_E2E_URL=https://localhost:8443 uv run pytest tests/e2e -m e2e

Самотест создаёт служебное событие на настоящих камерах-симуляторах: кадр и клип берутся из
GOP-буфера vision-pipeline, дальше обычный путь до каналов. Telegram и реле подменяет
alert-sink из compose.sim.yml. Проверяется:
- две камеры одного участка → один инцидент с клипом MP4 и ключевыми кадрами;
- алерт во всех каналах (веб, Telegram с кадром и клипом, HTTP-реле) в пределах SLO;
- Telegram недоступен → веб-UI и реле получают алерт, Telegram — после восстановления;
- канарейка пути алерта проходит.

Падение и дым на реальном видео (детектор, поза, FSM) проверяются, когда будут веса и ролики
samples/e2e — docs/TODO.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import urllib.request
from collections.abc import Iterator
from typing import Any

import av
import httpx
import pytest

from tests.e2e.conftest import SECRETS, URL, tls, wait_for

pytestmark = pytest.mark.e2e

SINK = "http://127.0.0.1:18080"
SINK_IN_NETWORK = "http://alert-sink:8080"
AREA = "e2e-press"
OPERATORS_CHAT = "-1001"
LEAD_CHAT = "-1002"
FAKE_TOKEN = "123456:e2e-sim-token"


def sink(path: str, method: str = "GET") -> Any:
    # Адрес фиксированный (127.0.0.1, порт alert-sink из compose.sim.yml) — схема всегда http.
    req = urllib.request.Request(  # noqa: S310
        f"{SINK}{path}", method=method, data=b"" if method == "POST" else None
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read())


class Viewer:
    """Открытое «окно UI»: WebSocket с сессией администратора. Алерт веб-UI считается
    доставленным, только если его кто-то видит."""

    def __init__(self, api: httpx.Client) -> None:
        from websockets.sync.client import connect

        cookie = "; ".join(f"{k}={v}" for k, v in api.cookies.items())
        self.ws = connect(
            f"{URL.replace('https://', 'wss://')}/api/v1/ws",
            ssl=tls(),
            additional_headers={"Cookie": cookie},
        )
        self.messages: list[dict[str, Any]] = []
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.ws:
                self.messages.append(json.loads(raw))

    def close(self) -> None:
        self.ws.close()


@pytest.fixture(scope="module")
def viewer(api: httpx.Client) -> Iterator[Viewer]:
    v = Viewer(api)
    yield v
    v.close()


@pytest.fixture(scope="module")
def configured(api: httpx.Client, viewer: Viewer) -> Iterator[list[str]]:
    try:
        sink("/_reset", "POST")
    except OSError:
        pytest.skip("alert-sink недоступен — запустите qoima sim up")
    cams = [c["camera"]["id"] for c in api.get("/api/v1/cameras").json() if c["camera"]["enabled"]]
    if len(cams) < 3:
        pytest.skip("нужно ≥ 3 подключённых камер (сначала приёмка Фазы 1)")
    original = api.get("/api/v1/setup/alerts").json()
    original.pop("telegram_token_set", None)
    zones_before = {cam: api.get(f"/api/v1/cameras/{cam}/zones").json() for cam in cams[:2]}
    try:
        alerts = json.loads(json.dumps(original))
        alerts["channels"]["telegram"].update(enabled=True, api_base=SINK_IN_NETWORK)
        alerts["channels"]["local_relay"]["enabled"] = True
        alerts["channels"]["local_relay"]["http"]["url"] = f"{SINK_IN_NETWORK}/relay"
        alerts["groups"]["shift_operators"]["telegram_chat_ids"] = [OPERATORS_CHAT]
        alerts["groups"]["shift_lead"]["telegram_chat_ids"] = [LEAD_CHAT]
        r = api.put("/api/v1/setup/alerts", json=alerts)
        assert r.status_code == 200, r.text
        token = api.put(
            "/api/v1/setup/alerts/telegram-token", json={"token": "123456:e2e-sim-token"}
        )
        assert token.status_code == 204, token.text
        for cam in cams[:2]:
            zone = {
                "id": f"{cam}-e2e",
                "camera_id": cam,
                "name": "Пресс (e2e)",
                "kind": "monitoring",
                "polygon": {
                    "points": [
                        {"x": 0.1, "y": 0.1},
                        {"x": 0.9, "y": 0.1},
                        {"x": 0.9, "y": 0.9},
                        {"x": 0.1, "y": 0.9},
                    ]
                },
                "area": AREA,
            }
            r = api.put(f"/api/v1/cameras/{cam}/zones", json={"zones": [zone]})
            assert r.status_code == 200, r.text
        yield cams
    finally:
        # Восстанавливаем, даже если настройка упала на полпути.
        with contextlib.suppress(OSError):
            sink("/_telegram?down=0", "POST")
        restored = api.put("/api/v1/setup/alerts", json=original)
        for cam, zones in zones_before.items():
            api.put(f"/api/v1/cameras/{cam}/zones", json={"zones": zones})
        # API удаления токена нет (его меняют, а не стирают) — убираем подставной файл сами.
        with contextlib.suppress(OSError):
            fake = SECRETS / "runtime" / "telegram_bot_token"
            if fake.read_text().strip() == FAKE_TOKEN:
                fake.unlink()
        assert restored.status_code == 200, restored.text


def selftest(api: httpx.Client, cams: list[str]) -> str:
    r = api.post("/api/v1/setup/selftest", json={"camera_ids": cams})
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


def status(api: httpx.Client, run_id: str) -> dict[str, Any]:
    r = api.get(f"/api/v1/setup/selftest/{run_id}")
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_two_cameras_one_area_one_incident_all_channels(
    api: httpx.Client, configured: list[str], viewer: Viewer
) -> None:
    run_id = selftest(api, configured[:2])
    done = wait_for(
        lambda: (s := status(api, run_id))["all_delivered"] and s["clip_ready"] and s,
        90,
        2,
        "доставка во все каналы и клип",
    )
    assert all(c["event_received"] and c["evidence"] for c in done["cameras"]), done["cameras"]
    assert len(done["incidents"]) == 1, done["incidents"]  # дедупликация по участку
    inc = done["incidents"][0]
    assert sorted(inc["camera_ids"]) == sorted(configured[:2]) and inc["area"] == AREA
    assert inc["within_slo"], inc
    channels = {(d["channel"], d["target"]) for d in inc["deliveries"]}
    assert ("web_ui", "web") in channels and ("local_relay", "http") in channels
    assert ("telegram", OPERATORS_CHAT) in channels and ("telegram", LEAD_CHAT) in channels
    # Клип — настоящий MP4 из GOP-буфера, читается декодером.
    clip = api.get(f"/api/v1/media/{inc['clip']['blob_key']}")
    assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
    with av.open(io.BytesIO(clip.content)) as container:
        frames = sum(1 for _ in container.decode(video=0))
    assert frames > 10
    assert inc["keyframes"] >= 1
    # Telegram получил кадр сразу и клип вдогонку; реле — JSON алерта.
    records = wait_for(
        lambda: (r := sink("/_records")) and any(x.get("method") == "sendVideo" for x in r) and r,
        30,
        1,
        "клип в Telegram",
    )
    photos = [x for x in records if x.get("method") == "sendPhoto"]
    assert {p["chat_id"] for p in photos} >= {OPERATORS_CHAT, LEAD_CHAT}
    assert all(p["file_bytes"] > 0 and "САМОТЕСТ" in p["text"] for p in photos)
    relay = [x for x in records if x["kind"] == "relay"]
    assert relay and relay[0]["body"]["incident_id"] == inc["incident_id"]
    # Окно UI получило алерт со звуком по WebSocket.
    ui_alerts = [m for m in viewer.messages if m.get("type") == "alert"]
    assert any(m.get("incident_id") == inc["incident_id"] and m.get("sound") for m in ui_alerts)
    confirmed = api.post(f"/api/v1/setup/selftest/{run_id}/confirm").json()
    assert confirmed["confirmed_at"]
    card = api.get(f"/api/v1/incidents/{inc['incident_id']}").json()
    assert card["incident"]["state"] == "RESOLVED"


def test_telegram_down_local_channels_still_work(api: httpx.Client, configured: list[str]) -> None:
    sink("/_reset", "POST")
    sink("/_telegram?down=1", "POST")
    run_id = selftest(api, [configured[2]])

    def local_sent() -> Any:
        s = status(api, run_id)
        if not s["incidents"]:
            return None
        ds = {(d["channel"], d["target"]): d for d in s["incidents"][0]["deliveries"]}
        ok = (
            ds.get(("web_ui", "web"), {}).get("status") == "sent"
            and ds.get(("local_relay", "http"), {}).get("status") == "sent"
        )
        return ok and ds

    ds = wait_for(local_sent, 30, 1, "веб-UI и реле при недоступном Telegram")
    assert ds[("telegram", OPERATORS_CHAT)]["status"] in ("pending", "retrying"), ds
    sink("/_telegram?down=0", "POST")
    wait_for(
        lambda: all(
            d["status"] == "sent" for d in status(api, run_id)["incidents"][0]["deliveries"]
        ),
        60,
        2,
        "доставка в Telegram после восстановления (повторы)",
    )
    api.post(f"/api/v1/setup/selftest/{run_id}/confirm")


def test_canary_alert_path(api: httpx.Client, configured: list[str]) -> None:
    r = api.post("/api/v1/setup/canary/run")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ok"] is True, result
    # Канарейка не попадает в ленту операторов.
    active = api.get("/api/v1/incidents", params={"scope": "active"}).json()
    assert all(i["incident"]["synthetic"] is None for i in active)
