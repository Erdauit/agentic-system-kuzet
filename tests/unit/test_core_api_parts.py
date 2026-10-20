"""Пароли, файл настроек объекта, синхронизация MediaMTX, каналы алертов, сетевые интерфейсы."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from cli.netinfo import parse_ifconfig, parse_ip_json
from core_api.alerts.channels import AlertMessage, ChannelDispatcher, send_http_relay, send_telegram
from core_api.auth.passwords import generate_password, hash_password, verify_password
from core_api.cameras.mediamtx import DesiredPath, MediaMtxClient
from core_api.site_config import SiteConfigStore
from shared.config import ConfigError, QoimaConfig
from shared.schemas import ChannelKind
from tests.conftest import CONFIG_DIR

MSG = AlertMessage("Тест", "проверка", "info")


def test_passwords() -> None:
    h = hash_password("правильный пароль")
    assert verify_password("правильный пароль", h)
    assert not verify_password("другой", h)
    assert not verify_password("x", None) and not verify_password("x", "plain")
    p = generate_password()
    assert len(p) == 16 and not set(p) & set("0O1lI")


def test_site_config_store(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, cfg_dir)
    store = SiteConfigStore(cfg_dir, tmp_path / "data" / "config" / "site.yaml")
    cfg = store.update({"defaults": {"site": {"name": "Цех №3"}}})
    assert cfg.defaults.site.name == "Цех №3"
    assert "Цех №3" in store.site_file.read_text()
    with pytest.raises(ConfigError):
        store.update({"defaults": {"site": {"timezone": "Mars/Base"}}})
    assert store.read()["defaults"]["site"]["name"] == "Цех №3"  # невалидное не записалось
    with pytest.raises(ConfigError, match="неизвестные"):
        store.update({"pipelines": {}})


async def test_mediamtx_reconcile() -> None:
    calls: list[tuple[str, str]] = []
    existing = {
        "items": [
            {"name": "cam-01/main", "source": "rtsp://old", "sourceOnDemand": False},
            {"name": "cam-09/sub", "source": "rtsp://gone", "sourceOnDemand": False},
            {"name": "manual-path", "source": "publisher"},
        ],
        "pageCount": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json=existing)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        stats = await MediaMtxClient("http://mtx:9997", http).reconcile(
            [DesiredPath("cam-01/main", "rtsp://new"), DesiredPath("cam-02/main", "rtsp://c2")]
        )
    assert stats == {"added": 1, "replaced": 1, "deleted": 1}
    assert ("POST", "/v3/config/paths/add/cam-02/main") in calls
    assert ("DELETE", "/v3/config/paths/delete/cam-09/sub") in calls
    assert not any("manual-path" in path for _m, path in calls)  # чужие пути не трогаем


async def test_channels(config: QoimaConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "telegram" in request.url.host:
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": body["chat_id"] != "bad"})
        return httpx.Response(204 if request.url.path == "/ok" else 500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        results = await send_telegram(http, "token", ["1", "bad"], MSG, 2)
        assert [r.ok for r in results] == [True, False]
        assert (await send_http_relay(http, "http://relay/ok", "POST", MSG, 2)).ok
        assert not (await send_http_relay(http, "http://relay/fail", "POST", MSG, 2)).ok
        dispatcher = ChannelDispatcher(http)
        disabled = await dispatcher.send(config.alerts, ChannelKind.TELEGRAM, ["engineers"], MSG)
        assert not disabled[0].ok and "выключен" in (disabled[0].error or "")
        assert (await dispatcher.send(config.alerts, ChannelKind.WEB_UI, [], MSG))[0].ok


def test_netinfo_parsers() -> None:
    ip_json = json.dumps(
        [
            {
                "ifname": "lo",
                "flags": ["UP"],
                "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}],
            },
            {
                "ifname": "eno1",
                "flags": ["UP"],
                "addr_info": [{"family": "inet", "local": "10.0.5.2", "prefixlen": 24}],
            },
            {
                "ifname": "docker0",
                "flags": ["UP"],
                "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}],
            },
            {
                "ifname": "eno2",
                "flags": ["DOWN"],
                "addr_info": [{"family": "inet", "local": "10.9.0.2", "prefixlen": 24}],
            },
        ]
    )
    assert [(a.interface, str(a.network)) for a in parse_ip_json(ip_json)] == [
        ("eno1", "10.0.5.0/24")
    ]
    ifconfig = (
        "lo0: flags=8049<UP>\n\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags=8863<UP>\n\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255\n"
        "bridge100: flags=8863<UP>\n\tinet 192.168.64.1 netmask 0xffffff00\n"
    )
    assert [str(a.network) for a in parse_ifconfig(ifconfig)] == ["192.168.1.0/24"]
