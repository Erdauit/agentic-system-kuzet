"""Автопоиск камер в сети объекта (SPEC.md §3.2, шаг 3).

Три способа, которые работают и из Docker-контейнера за NAT:
1. WS-Discovery multicast (239.255.255.250:3702) — находит ONVIF-устройства в том же L2-сегменте;
2. WS-Discovery адресными (unicast) запросами на каждый адрес подсетей — проходит через NAT
   контейнера, поэтому камеры находятся и без host-сети;
3. TCP-сканирование RTSP- и ONVIF-портов — для камер без ONVIF или с выключенным discovery.

Найденные ONVIF-устройства опрашиваются (модель, профили, адреса потоков) с учётными данными,
которые ввёл администратор. Для устройств без ONVIF кандидаты потоков строятся по шаблонам
производителей. Окончательный выбор main/sub делает зондирование (probe.py).

Поиск запускается только вручную: сканирование сети завода не должно идти фоном.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from core_api.cameras.onvif import (
    WS_DISCOVERY_ADDR,
    Credentials,
    OnvifAuthError,
    OnvifClient,
    OnvifError,
    ProbeMatch,
    parse_probe_matches,
    probe_message,
)
from shared.config.models import CameraUrlTemplates, DiscoveryConfig
from shared.schemas import (
    CandidateSource,
    DiscoveredDevice,
    DiscoveryMethod,
    StreamCandidate,
    StreamRole,
)
from shared.telemetry import get_logger

log = get_logger("discovery")

Progress = Callable[[int, int], Awaitable[None]]

# Пути, которые часто встречаются у камер без ONVIF и неизвестного производителя.
GENERIC_PATHS = (
    "/",
    "/live",
    "/stream1",
    "/stream2",
    "/h264",
    "/live/ch00_0",
    "/live/ch00_1",
    "/11",
    "/12",
)


# ---------------------------------------------------------------- подсети


@dataclass(frozen=True)
class ScanPlan:
    hosts: list[ipaddress.IPv4Address]
    subnets: list[str]
    warnings: list[str] = field(default_factory=list)


def default_route_interfaces(route_table: str) -> set[str]:
    """Интерфейсы с маршрутом по умолчанию (/proc/net/route). Внутренняя сеть backend
    (internal: true) маршрута наружу не имеет — камер в ней не бывает, её не сканируем."""
    names = set()
    for line in route_table.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 2 and cols[1] == "00000000":
            names.add(cols[0])
    return names


def _local_ipv4_interfaces() -> list[ipaddress.IPv4Interface]:
    """Адреса интерфейсов контейнера с маршрутом наружу (Linux, ioctl SIOCGIFADDR/SIOCGIFNETMASK)."""
    import fcntl  # Linux-only; на macOS/Windows сюда не попадаем

    result: list[ipaddress.IPv4Interface] = []
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        return result
    try:
        routed = default_route_interfaces(Path("/proc/net/route").read_text())
    except OSError:
        routed = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for name in sorted(p.name for p in net_dir.iterdir()):
            if name == "lo" or (routed and name not in routed):
                continue
            req = struct.pack("256s", name[:15].encode())
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])
            except OSError:
                continue
            result.append(ipaddress.IPv4Interface(f"{addr}/{mask}"))
    return result


def plan_scan(
    configured: Iterable[str],
    host_subnets: Iterable[str],
    local_interfaces: Iterable[ipaddress.IPv4Interface],
    exclude: Iterable[ipaddress.IPv4Address],
    max_hosts: int,
) -> ScanPlan:
    """Список адресов для сканирования.

    - подсети из конфига и подсети сервера (их передаёт `qoima up`) — целиком;
    - сети самого контейнера — только /24 вокруг его адреса (сети Docker бывают /16; камеры
      симулятора и соседние контейнеры живут в первых адресах);
    - сеть больше max_hosts урезается до /24 вокруг адреса сервера, с предупреждением.
    """
    warnings: list[str] = []
    nets: list[ipaddress.IPv4Network] = []
    own: set[ipaddress.IPv4Address] = set()

    def add(net: ipaddress.IPv4Network) -> None:
        if net.prefixlen < 16 or net.is_loopback or net.is_link_local or net.is_multicast:
            warnings.append(f"подсеть {net} пропущена: слишком большая или служебная")
            return
        if not any(net.subnet_of(n) for n in nets):
            nets[:] = [n for n in nets if not n.subnet_of(net)]
            nets.append(net)

    for raw in [*configured, *host_subnets]:
        try:
            net = ipaddress.ip_network(raw.strip(), strict=False)
        except ValueError:
            warnings.append(f"некорректная подсеть {raw!r} пропущена")
            continue
        if not isinstance(net, ipaddress.IPv4Network):
            warnings.append(f"IPv6-подсеть {net} пока не сканируется")
            continue
        add(net)
    for iface in local_interfaces:
        own.add(iface.ip)
        add(ipaddress.IPv4Network(f"{iface.ip}/24", strict=False))

    hosts: list[ipaddress.IPv4Address] = []
    excluded = set(exclude) | own
    for net in nets:
        addrs = list(net.hosts()) if net.num_addresses > 2 else list(net)
        if len(hosts) + len(addrs) > max_hosts:
            narrowed = ipaddress.IPv4Network(f"{net.network_address}/24", strict=False)
            warnings.append(
                f"подсеть {net} больше лимита {max_hosts} адресов — сканируется только {narrowed}; "
                "добавьте подсети камер в discovery.subnets"
            )
            addrs = list(narrowed.hosts())
        hosts.extend(a for a in addrs if a not in excluded)
    unique = list(dict.fromkeys(hosts))[:max_hosts]
    return ScanPlan(hosts=unique, subnets=[str(n) for n in nets], warnings=warnings)


# ---------------------------------------------------------------- WS-Discovery и порты


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.matches: dict[str, list[ProbeMatch]] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        try:
            matches = parse_probe_matches(data)
        except OnvifError:
            return
        if matches:
            self.matches.setdefault(str(addr[0]), []).extend(matches)


async def ws_discovery(
    hosts: Iterable[ipaddress.IPv4Address], timeout_s: float, *, multicast: bool = True
) -> dict[str, list[ProbeMatch]]:
    """Probe на multicast-адрес и на каждый адрес из hosts; ответы собираются timeout_s секунд."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        local_addr=("0.0.0.0", 0),  # noqa: S104 — ответы WS-Discovery приходят на любой интерфейс
    )
    try:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        message = probe_message()
        if multicast:
            with contextlib.suppress(OSError):
                transport.sendto(message, WS_DISCOVERY_ADDR)
        for i, host in enumerate(hosts):
            with contextlib.suppress(OSError):
                transport.sendto(message, (str(host), WS_DISCOVERY_ADDR[1]))
            if i % 64 == 63:
                await asyncio.sleep(0.01)  # не заливаем сеть пакетами разом
        await asyncio.sleep(timeout_s)
    finally:
        transport.close()
    return protocol.matches


async def tcp_open(host: str, port: int, timeout_s: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def port_scan(
    hosts: list[ipaddress.IPv4Address],
    ports: list[int],
    timeout_s: float,
    concurrency: int,
    progress: Progress | None = None,
) -> dict[str, set[int]]:
    sem = asyncio.Semaphore(concurrency)
    found: dict[str, set[int]] = {}
    done = 0

    async def check(host: str) -> None:
        nonlocal done
        for port in ports:
            async with sem:
                if await tcp_open(host, port, timeout_s):
                    found.setdefault(host, set()).add(port)
        done += 1
        if progress is not None and done % 32 == 0:
            await progress(done, len(hosts))

    await asyncio.gather(*(check(str(h)) for h in hosts))
    if progress is not None:
        await progress(len(hosts), len(hosts))
    return found


# ---------------------------------------------------------------- кандидаты потоков


def strip_credentials(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def vendor_key(manufacturer: str | None, templates: CameraUrlTemplates) -> str | None:
    if not manufacturer:
        return None
    norm = manufacturer.lower().replace(" ", "-")
    for key in templates.vendors:
        if norm.startswith(key) or key in norm:
            return key
    return None


def template_candidates(
    host: str, port: int, templates: CameraUrlTemplates, vendor: str | None, channels: int = 1
) -> list[StreamCandidate]:
    keys = [vendor] if vendor else list(templates.vendors)
    out: list[StreamCandidate] = []
    for key in keys:
        if key is None:
            continue
        tpl = templates.vendors[key]
        for ch in range(1, channels + 1):
            out.append(
                StreamCandidate(
                    url=tpl.main.format(host=host, port=port, channel=ch),
                    source=CandidateSource.VENDOR_TEMPLATE,
                    role_hint=StreamRole.MAIN,
                    channel=ch,
                )
            )
            for sub in tpl.sub:
                out.append(
                    StreamCandidate(
                        url=sub.format(host=host, port=port, channel=ch),
                        source=CandidateSource.VENDOR_TEMPLATE,
                        role_hint=StreamRole.SUB,
                        channel=ch,
                    )
                )
    if vendor is None:
        out.extend(
            StreamCandidate(url=f"rtsp://{host}:{port}{p}", source=CandidateSource.GENERIC)
            for p in GENERIC_PATHS
        )
    return list({c.url: c for c in out}.values())


@dataclass
class OnvifResult:
    device: DiscoveredDevice
    credentials: Credentials | None
    # Устройство действительно говорит на ONVIF (ответило SOAP или отказом в доступе), а не
    # просто держит открытый порт 80, как принтер или роутер.
    responded: bool


async def inspect_onvif(
    host: str,
    xaddrs: list[str],
    methods: list[DiscoveryMethod],
    credentials: list[Credentials],
    http: httpx.AsyncClient,
    match: ProbeMatch | None,
    rtsp_port: int | None,
) -> OnvifResult:
    """Опрос ONVIF-устройства: перебирает учётные данные, собирает профили и адреса потоков."""
    errors: list[str] = []
    name = match.scope("name") if match else None
    tried_auth = False
    responded = match is not None
    for xaddr in xaddrs:
        for creds in [*credentials, None]:
            client = OnvifClient(xaddr, creds, http)
            try:
                with contextlib.suppress(OnvifError):
                    await client.sync_clock()
                info = await client.device_info()
                profiles = await client.profiles()
                candidates: list[StreamCandidate] = []
                sources: dict[str, int] = {}
                for prof in profiles:
                    uri = await client.stream_uri(prof.token)
                    if uri is None:
                        continue
                    channel = sources.setdefault(prof.source_token or "default", len(sources) + 1)
                    candidates.append(
                        StreamCandidate(
                            url=strip_credentials(uri),
                            source=CandidateSource.ONVIF_PROFILE,
                            profile=prof.name or prof.token,
                            width=prof.width,
                            height=prof.height,
                            channel=min(channel, 256),
                        )
                    )
                port = rtsp_port
                if port is None and candidates:
                    port = urlsplit(candidates[0].url).port or 554
                device = DiscoveredDevice(
                    host=host,
                    methods=methods,
                    onvif_xaddr=xaddr,
                    rtsp_port=port,
                    manufacturer=info.manufacturer,
                    model=info.model,
                    firmware=info.firmware,
                    serial=info.serial,
                    name=name,
                    auth="ok" if creds is not None else "not_required",
                    channels=max(1, len(sources)),
                    candidates=candidates,
                    errors=errors,
                )
                return OnvifResult(device, creds, responded=True)
            except OnvifAuthError:
                tried_auth = True
                responded = True
                continue
            except OnvifError as exc:
                errors.append(f"{urlsplit(xaddr).netloc}: {exc}")
                break
    device = DiscoveredDevice(
        host=host,
        methods=methods,
        onvif_xaddr=xaddrs[0] if xaddrs else None,
        rtsp_port=rtsp_port,
        name=name,
        auth="failed" if tried_auth else "unknown",
        errors=[*errors, "ONVIF: не подошли учётные данные"] if tried_auth else errors,
    )
    return OnvifResult(device, None, responded=responded)


@dataclass
class DiscoveryOutcome:
    devices: list[DiscoveredDevice]
    credentials: dict[str, Credentials]  # host → подошедшие учётные данные (только в памяти)
    plan: ScanPlan


async def discover(
    plan: ScanPlan,
    cfg: DiscoveryConfig,
    templates: CameraUrlTemplates,
    credentials: list[Credentials],
    progress: Progress | None = None,
) -> DiscoveryOutcome:
    multicast_task = asyncio.create_task(ws_discovery(plan.hosts, cfg.ws_discovery_timeout_s))
    ports = sorted(set(cfg.rtsp_ports) | set(cfg.onvif_ports))
    open_ports = await port_scan(
        plan.hosts, ports, cfg.connect_timeout_s, cfg.concurrency, progress
    )
    matches = await multicast_task

    devices: list[DiscoveredDevice] = []
    found_creds: dict[str, Credentials] = {}
    hosts = sorted(set(matches) | set(open_ports), key=lambda h: ipaddress.ip_address(h))
    async with httpx.AsyncClient(timeout=cfg.onvif_timeout_s, verify=False) as http:  # noqa: S501 — камеры с самоподписанными сертификатами
        sem = asyncio.Semaphore(8)

        async def handle(host: str) -> None:
            async with sem:
                ports_here = open_ports.get(host, set())
                rtsp_port = next((p for p in cfg.rtsp_ports if p in ports_here), None)
                methods: list[DiscoveryMethod] = []
                xaddrs: list[str] = []
                match = matches.get(host, [None])[0]
                if match is not None:
                    methods.append(DiscoveryMethod.ONVIF_UNICAST)
                    xaddrs.extend(match.xaddrs)
                for p in cfg.onvif_ports:
                    if p in ports_here:
                        xaddrs.append(f"http://{host}:{p}/onvif/device_service")
                xaddrs = list(dict.fromkeys(xaddrs))
                if rtsp_port is not None:
                    methods.append(DiscoveryMethod.RTSP_SCAN)
                if not methods and not xaddrs:
                    return
                base: DiscoveredDevice | None = None
                if xaddrs:
                    result = await inspect_onvif(
                        host,
                        xaddrs,
                        methods or [DiscoveryMethod.ONVIF_UNICAST],
                        credentials,
                        http,
                        match,
                        rtsp_port,
                    )
                    if result.credentials is not None:
                        found_creds[host] = result.credentials
                    if result.device.candidates:
                        devices.append(result.device)
                        return
                    if result.responded:
                        base = result.device
                        if rtsp_port is None:
                            # ONVIF есть, но потоки не отданы (нет прав) — показываем, чтобы ввести пароль.
                            devices.append(base)
                            return
                if rtsp_port is None:
                    return  # открыт только веб-порт и это не ONVIF — не камера
                if base is None:
                    base = DiscoveredDevice(
                        host=host, methods=[DiscoveryMethod.RTSP_SCAN], rtsp_port=rtsp_port
                    )
                vendor = vendor_key(base.manufacturer, templates)
                candidates = template_candidates(host, rtsp_port, templates, vendor, base.channels)
                devices.append(
                    base.model_copy(update={"candidates": candidates, "rtsp_port": rtsp_port})
                )

        await asyncio.gather(*(handle(h) for h in hosts))
    devices.sort(key=lambda d: ipaddress.ip_address(d.host))
    log.info("discovery_done", hosts=len(plan.hosts), devices=len(devices))
    return DiscoveryOutcome(devices=devices, credentials=found_creds, plan=plan)
