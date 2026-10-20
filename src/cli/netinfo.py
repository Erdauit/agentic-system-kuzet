"""Сетевые интерфейсы сервера: подсети для автопоиска камер и адреса для WebRTC.

core-api работает в контейнере и не видит сети хоста, поэтому `qoima up` передаёт ему
подсети физических интерфейсов сервера (QOIMA_HOST_SUBNETS) и их адреса (QOIMA_HOST_IPS).
"""

from __future__ import annotations

import ipaddress
import json
import platform
import re
from dataclasses import dataclass

from cli.runner import Runner

# Виртуальные интерфейсы Docker, VPN и т.п. — камер за ними нет.
_SKIP = re.compile(
    r"^(lo\d*|docker\d*|br-.*|veth.*|virbr.*|cni.*|flannel.*|cali.*|tun\d*|tap\d*|wg\d*|utun\d*|"
    r"awdl\d*|llw\d*|bridge\d*|vmenet\d*|anpi\d*|zt.*|tailscale\d*)$"
)


@dataclass(frozen=True)
class HostAddress:
    interface: str
    ip: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


def _keep(name: str, ip: ipaddress.IPv4Address) -> bool:
    return not _SKIP.match(name) and not ip.is_loopback and not ip.is_link_local


def parse_ip_json(output: str) -> list[HostAddress]:
    """Вывод `ip -j -4 addr show` (Linux)."""
    result = []
    for iface in json.loads(output or "[]"):
        name = str(iface.get("ifname", ""))
        if "UP" not in iface.get("flags", []):
            continue
        for a in iface.get("addr_info", []):
            if a.get("family") not in (None, "inet"):
                continue
            ip = ipaddress.IPv4Address(a["local"])
            if _keep(name, ip):
                result.append(
                    HostAddress(
                        name, ip, ipaddress.IPv4Network(f"{ip}/{a['prefixlen']}", strict=False)
                    )
                )
    return result


def parse_ifconfig(output: str) -> list[HostAddress]:
    """Вывод `ifconfig` (macOS/BSD) — для режима разработки."""
    result = []
    current = ""
    for line in output.splitlines():
        if line and not line[0].isspace():
            current = line.split(":", 1)[0]
            continue
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        ip = ipaddress.IPv4Address(m.group(1))
        mask = m.group(2)
        prefix = (
            bin(int(mask, 16)).count("1")
            if mask.startswith("0x")
            else ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        )
        if _keep(current, ip):
            result.append(
                HostAddress(current, ip, ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False))
            )
    return result


def host_addresses(runner: Runner) -> list[HostAddress]:
    if platform.system() == "Linux":
        res = runner(["ip", "-j", "-4", "addr", "show"])
        return parse_ip_json(res.stdout) if res.ok else []
    res = runner(["ifconfig"])
    return parse_ifconfig(res.stdout) if res.ok else []
