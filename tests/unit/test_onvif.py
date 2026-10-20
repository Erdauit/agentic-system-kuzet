"""ONVIF-клиент против сервера симулятора камеры (docker/camera-sim/onvif_sim.py) в процессе:
WS-Security PasswordDigest, профили, адреса потоков, отказ при неверном пароле, защита XML."""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from core_api.cameras.onvif import (
    Credentials,
    OnvifAuthError,
    OnvifClient,
    OnvifError,
    parse_probe_matches,
    parse_xml,
    probe_message,
)

SIM = Path(__file__).resolve().parents[2] / "docker" / "camera-sim" / "onvif_sim.py"


def load_sim() -> ModuleType:
    spec = importlib.util.spec_from_file_location("onvif_sim", SIM)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sim() -> Iterator[tuple[ModuleType, str]]:
    module = load_sim()
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield module, f"http://127.0.0.1:{server.server_address[1]}/onvif/device_service"
    server.shutdown()


async def test_profiles_and_stream_uris(sim: tuple[ModuleType, str]) -> None:
    module, xaddr = sim
    async with httpx.AsyncClient(timeout=5) as http:
        client = OnvifClient(xaddr, Credentials(module.USER, module.PASSWORD), http)
        await client.sync_clock()
        info = await client.device_info()
        assert info.manufacturer == "Qoima Sim"
        client.media_xaddr = xaddr  # media service симулятора на том же порту
        profiles = await client.profiles()
        assert [(p.name, p.width, p.height) for p in profiles] == [
            ("mainStream", 1280, 720),
            ("subStream", 640, 360),
        ]
        uri = await client.stream_uri(profiles[1].token)
        assert uri is not None and uri.endswith("/Streaming/Channels/102")


async def test_wrong_password_rejected(sim: tuple[ModuleType, str]) -> None:
    module, xaddr = sim
    async with httpx.AsyncClient(timeout=5) as http:
        client = OnvifClient(xaddr, Credentials(module.USER, "wrong"), http)
        with pytest.raises(OnvifAuthError):
            await client.device_info()


def test_probe_match_round_trip() -> None:
    module = load_sim()
    matches = parse_probe_matches(module.probe_match("uuid:1"))
    assert len(matches) == 1
    assert matches[0].xaddrs[0].endswith("/onvif/device_service")
    assert (
        matches[0]
        .scope(
            "name",
        )
        .startswith("Qoima Sim")
    )  # type: ignore[union-attr]
    assert b"NetworkVideoTransmitter" in probe_message()


def test_xml_entities_rejected() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(OnvifError, match="DOCTYPE"):
        parse_xml(evil)
