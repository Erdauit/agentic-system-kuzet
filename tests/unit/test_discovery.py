"""Планирование сканирования, кандидаты потоков, выбор main/sub."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime

import pytest

from core_api.cameras.discovery import (
    default_route_interfaces,
    plan_scan,
    strip_credentials,
    template_candidates,
    vendor_key,
)
from core_api.cameras.onvif import Credentials
from core_api.cameras.probe import ProbedCandidate, ProbeResult, choose_streams, with_credentials
from core_api.routers.cameras import parse_import
from shared.config import QoimaConfig
from shared.schemas import CandidateSource, StreamCandidate, StreamProbe, StreamRole, VideoCodec


def test_plan_scan_rules() -> None:
    plan = plan_scan(
        ["10.0.5.0/24", "bad", "fe80::/64"],
        ["192.168.1.0/24"],
        [ipaddress.IPv4Interface("172.20.0.5/16")],
        [ipaddress.IPv4Address("172.20.0.2")],
        max_hosts=4096,
    )
    hosts = set(map(str, plan.hosts))
    assert "10.0.5.17" in hosts and "192.168.1.10" in hosts
    assert "172.20.0.3" in hosts  # сеть контейнера — только /24 вокруг его адреса
    assert "172.20.1.3" not in hosts
    assert "172.20.0.5" not in hosts and "172.20.0.2" not in hosts  # свой адрес и MediaMTX
    assert any("bad" in w for w in plan.warnings) and any("IPv6" in w for w in plan.warnings)


def test_plan_scan_caps_large_networks() -> None:
    plan = plan_scan(["10.10.0.0/16"], [], [], [], max_hosts=4096)
    assert len(plan.hosts) == 254
    assert any("больше лимита" in w for w in plan.warnings)


def test_default_route_interfaces() -> None:
    table = "Iface\tDestination\tGateway\neth1\t00000000\t010014AC\neth0\t000013AC\t00000000\n"
    assert default_route_interfaces(table) == {"eth1"}


def test_templates(config: QoimaConfig) -> None:
    templates = config.camera_url_templates
    assert vendor_key("HIKVISION", templates) == "hikvision"
    assert vendor_key("Qoima Sim", templates) == "qoima-sim"
    assert vendor_key("ACME", templates) is None
    known = template_candidates("10.0.0.9", 554, templates, "dahua")
    assert {c.role_hint for c in known} == {StreamRole.MAIN, StreamRole.SUB}
    assert all("cam/realmonitor" in c.url for c in known)
    unknown = template_candidates("10.0.0.9", 554, templates, None)
    assert any(c.source is CandidateSource.GENERIC for c in unknown)
    assert len({c.url for c in unknown}) == len(unknown)


def test_credentials_in_urls() -> None:
    url = with_credentials("rtsp://10.0.0.9:554/a", Credentials("adm in", "p@ss:w/d"))
    assert url == "rtsp://adm%20in:p%40ss%3Aw%2Fd@10.0.0.9:554/a"
    assert strip_credentials(url) == "rtsp://10.0.0.9:554/a"


def _probe(url: str, w: int, h: int, codec: VideoCodec = VideoCodec.H264) -> ProbedCandidate:
    p = StreamProbe(
        role=StreamRole.MAIN,
        url=url,
        codec=codec,
        width=w,
        height=h,
        fps=15,
        probed_at=datetime.now(UTC),
    )
    return ProbedCandidate(
        StreamCandidate(url=url, source=CandidateSource.ONVIF_PROFILE),
        ProbeResult(p, None, None),
        None,
    )


def test_choose_streams() -> None:
    a = _probe("rtsp://h/main", 1920, 1080)
    b = _probe("rtsp://h/sub", 640, 360)
    failed = ProbedCandidate(
        StreamCandidate(url="rtsp://h/x", source=CandidateSource.GENERIC), None, "404"
    )
    report, _main, _sub = choose_streams("h", 1, [failed, b, a], 720)
    assert (
        report.main and report.main.url == "rtsp://h/main" and report.main.role is StreamRole.MAIN
    )
    assert report.sub and report.sub.url == "rtsp://h/sub" and report.sub.role is StreamRole.SUB
    assert len(report.tried) == 3 and not report.warnings
    only_big, _, _ = choose_streams("h", 1, [a], 720)
    assert only_big.sub is None and "substream" in only_big.warnings[0]
    only_small, _, sub2 = choose_streams("h", 1, [b], 720)
    assert only_small.sub is not None and sub2 is not None
    h265, _, _ = choose_streams("h", 1, [_probe("rtsp://h/m", 1920, 1080, VideoCodec.H265)], 720)
    assert any("H.265" in w for w in h265.warnings)


def test_parse_import_csv_and_yaml() -> None:
    rows = parse_import("csv", "name;main_url;username;password\nВход;rtsp://10.0.0.1/a;admin;x\n")
    assert rows == [
        {"name": "Вход", "main_url": "rtsp://10.0.0.1/a", "username": "admin", "password": "x"}
    ]
    rows = parse_import("yaml", "cameras:\n  - {name: Склад, url: 'rtsp://10.0.0.2/a'}\n")
    assert rows[0]["main_url"] == "rtsp://10.0.0.2/a"
    with pytest.raises(ValueError, match="main_url"):
        parse_import("csv", "name\nбез адреса\n")
