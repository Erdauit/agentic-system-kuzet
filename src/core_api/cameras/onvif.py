"""Минимальный ONVIF-клиент: WS-Discovery, Device и Media (Profile S).

Своя реализация вместо onvif-zeep: нужны 5 вызовов, а zeep тянет большой стек зависимостей.
Ответы камер — недоверенные данные: XML с DOCTYPE/ENTITY отвергается (защита от XXE и
«billion laughs»), разбор ведётся по локальным именам элементов, потому что производители
по-разному объявляют пространства имён.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

import httpx

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)

_NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


class OnvifError(Exception):
    pass


class OnvifAuthError(OnvifError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


# ---------------------------------------------------------------- XML


def parse_xml(data: bytes) -> ET.Element:
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise OnvifError("ответ устройства содержит DOCTYPE/ENTITY — отклонён")
    try:
        return ET.fromstring(data)  # noqa: S314 — DOCTYPE/ENTITY отклонены выше
    except ET.ParseError as exc:
        raise OnvifError(f"некорректный XML от устройства: {exc}") from exc


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in root.iter() if local(el.tag) == name]


def find_text(root: ET.Element, *path: str) -> str | None:
    """Текст первого элемента по цепочке локальных имён (поиск в глубину на каждом шаге)."""
    nodes = [root]
    for name in path:
        nodes = [
            child
            for node in nodes
            for child in node.iter()
            if local(child.tag) == name and child is not node
        ]
        if not nodes:
            return None
    text = nodes[0].text
    return text.strip() if text else None


# ---------------------------------------------------------------- WS-Discovery


def probe_message(message_id: str | None = None) -> bytes:
    mid = message_id or f"uuid:{uuid.uuid4()}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="{_NS_SOAP}" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>{mid}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>""".encode()


@dataclass(frozen=True)
class ProbeMatch:
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]

    def scope(self, kind: str) -> str | None:
        prefix = f"onvif://www.onvif.org/{kind}/"
        for s in self.scopes:
            if s.lower().startswith(prefix):
                return unquote(s[len(prefix) :]).replace("_", " ").strip() or None
        return None


def parse_probe_matches(data: bytes) -> list[ProbeMatch]:
    root = parse_xml(data)
    matches = []
    for pm in find_all(root, "ProbeMatch"):
        xaddrs = find_text(pm, "XAddrs") or ""
        scopes = find_text(pm, "Scopes") or ""
        addrs = tuple(a for a in xaddrs.split() if a.startswith(("http://", "https://")))
        if addrs:
            matches.append(ProbeMatch(xaddrs=addrs, scopes=tuple(scopes.split())))
    return matches


# ---------------------------------------------------------------- SOAP


def security_header(creds: Credentials, now: datetime, nonce: bytes | None = None) -> str:
    """WS-Security UsernameToken с PasswordDigest = Base64(SHA1(nonce + created + password))."""
    raw_nonce = nonce if nonce is not None else os.urandom(16)
    created = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(  # noqa: S324 — SHA-1 требует стандарт WS-Security UsernameToken
            raw_nonce + created.encode() + creds.password.encode(), usedforsecurity=True
        ).digest()
    ).decode()
    return (
        f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}" s:mustUnderstand="1"><wsse:UsernameToken>'
        f"<wsse:Username>{escape(creds.username)}</wsse:Username>"
        f'<wsse:Password Type="{_DIGEST}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_B64}">{base64.b64encode(raw_nonce).decode()}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
    )


def envelope(body: str, header: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{_NS_SOAP}" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def is_auth_fault(root: ET.Element) -> bool:
    texts = " ".join(
        (el.text or "")
        for el in root.iter()
        if local(el.tag) in {"Value", "Subcode", "Text", "Reason"}
    )
    return any(
        marker in texts
        for marker in ("NotAuthorized", "FailedAuthentication", "Sender not Authorized")
    )


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str | None
    model: str | None
    firmware: str | None
    serial: str | None


@dataclass(frozen=True)
class MediaProfile:
    token: str
    name: str | None
    encoding: str | None
    width: int | None
    height: int | None
    fps: float | None
    source_token: str | None


def parse_device_info(root: ET.Element) -> DeviceInfo:
    return DeviceInfo(
        manufacturer=find_text(root, "Manufacturer"),
        model=find_text(root, "Model"),
        firmware=find_text(root, "FirmwareVersion"),
        serial=find_text(root, "SerialNumber"),
    )


def parse_media_xaddr(root: ET.Element) -> str | None:
    return find_text(root, "Media", "XAddr")


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None


def parse_profiles(root: ET.Element) -> list[MediaProfile]:
    profiles = []
    for p in find_all(root, "Profiles"):
        token = p.attrib.get("token")
        if not token:
            continue
        vec = next((el for el in p.iter() if local(el.tag) == "VideoEncoderConfiguration"), None)
        vsc = next((el for el in p.iter() if local(el.tag) == "VideoSourceConfiguration"), None)
        fps_text = find_text(vec, "FrameRateLimit") if vec is not None else None
        profiles.append(
            MediaProfile(
                token=token,
                name=find_text(p, "Name"),
                encoding=find_text(vec, "Encoding") if vec is not None else None,
                width=_int(find_text(vec, "Width")) if vec is not None else None,
                height=_int(find_text(vec, "Height")) if vec is not None else None,
                fps=float(fps_text)
                if fps_text and fps_text.replace(".", "", 1).isdigit()
                else None,
                source_token=find_text(vsc, "SourceToken") if vsc is not None else None,
            )
        )
    return profiles


def parse_system_time(root: ET.Element) -> datetime | None:
    utc = next((el for el in root.iter() if local(el.tag) == "UTCDateTime"), None)
    if utc is None:
        return None
    parts = [_int(find_text(utc, n)) for n in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
    if any(p is None for p in parts):
        return None
    y, mo, d, h, mi, s = (int(p) for p in parts if p is not None)
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=UTC)
    except ValueError:
        return None


class OnvifClient:
    """Клиент одного устройства. Время для WS-Security берётся с поправкой на часы камеры:
    у камер часто «уплывшее» время, а UsernameToken с расхождением > 5 мин отвергается."""

    def __init__(self, xaddr: str, creds: Credentials | None, http: httpx.AsyncClient) -> None:
        self.xaddr = xaddr
        self.creds = creds
        self.http = http
        self.clock_offset = timedelta(0)
        self.media_xaddr: str | None = None

    async def _call(self, url: str, body: str, *, auth: bool) -> ET.Element:
        header = ""
        if auth and self.creds is not None:
            header = security_header(self.creds, datetime.now(UTC) + self.clock_offset)
        try:
            resp = await self.http.post(
                url,
                content=envelope(body, header),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise OnvifError(f"{type(exc).__name__}: {exc}") from exc
        root = parse_xml(resp.content) if resp.content else None
        if resp.status_code in (401, 403) or (
            root is not None and resp.status_code >= 400 and is_auth_fault(root)
        ):
            raise OnvifAuthError("устройство отклонило учётные данные")
        if resp.status_code >= 400 or root is None:
            raise OnvifError(f"HTTP {resp.status_code} от {urlsplit(url).netloc}")
        return root

    async def sync_clock(self) -> None:
        root = await self._call(self.xaddr, "<tds:GetSystemDateAndTime/>", auth=False)
        device_time = parse_system_time(root)
        if device_time is not None:
            self.clock_offset = device_time - datetime.now(UTC)

    async def device_info(self) -> DeviceInfo:
        return parse_device_info(
            await self._call(self.xaddr, "<tds:GetDeviceInformation/>", auth=True)
        )

    async def media_service(self) -> str:
        if self.media_xaddr is None:
            root = await self._call(
                self.xaddr,
                "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
                auth=True,
            )
            self.media_xaddr = parse_media_xaddr(root) or self.xaddr
        return self.media_xaddr

    async def profiles(self) -> list[MediaProfile]:
        return parse_profiles(
            await self._call(await self.media_service(), "<trt:GetProfiles/>", auth=True)
        )

    async def stream_uri(self, profile_token: str) -> str | None:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(profile_token)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        root = await self._call(await self.media_service(), body, auth=True)
        uri = find_text(root, "MediaUri", "Uri")
        return uri if uri and uri.lower().startswith(("rtsp://", "rtsps://")) else None
