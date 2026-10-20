"""ONVIF-часть симулятора камеры (только stdlib): WS-Discovery и минимальный Profile S.

- UDP 3702: отвечает на Probe (multicast 239.255.255.250 и адресные запросы) — ProbeMatches
  с адресом device service и scopes (name, hardware);
- HTTP 80: GetSystemDateAndTime (без авторизации), GetDeviceInformation, GetCapabilities,
  GetProfiles, GetStreamUri — с проверкой WS-Security UsernameToken (PasswordDigest).

Используется только для разработки и e2e-тестов автопоиска (`qoima sim`).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER = os.environ.get("SIM_USER", "admin")
PASSWORD = os.environ.get("SIM_PASSWORD", "qoima-sim")
HOSTNAME = socket.gethostname()
DEVICE_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, HOSTNAME)
MULTICAST = ("239.255.255.250", 3702)

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
PROFILES = [
    ("Profile_1", "mainStream", 1280, 720, "Streaming/Channels/101"),
    ("Profile_2", "subStream", 640, 360, "Streaming/Channels/102"),
]


def own_ip() -> str:
    try:
        return socket.gethostbyname(HOSTNAME)
    except OSError:
        return "127.0.0.1"


def envelope(body: str, relates_to: str | None = None) -> bytes:
    header = ""
    if relates_to:
        header = (
            '<w:MessageID xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
            f"uuid:{uuid.uuid4()}</w:MessageID>"
            '<w:RelatesTo xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
            f"{relates_to}</w:RelatesTo>"
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP_NS}" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


# ---------------------------------------------------------------- WS-Discovery


def probe_match(message_id: str) -> bytes:
    xaddr = f"http://{own_ip()}:80/onvif/device_service"
    scopes = " ".join(
        [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/Profile/Streaming",
            f"onvif://www.onvif.org/name/Qoima_Sim_{HOSTNAME}",
            "onvif://www.onvif.org/hardware/QSIM-1",
            "onvif://www.onvif.org/location/simulator",
        ]
    )
    body = (
        "<d:ProbeMatches><d:ProbeMatch>"
        f'<w:EndpointReference xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
        f"<w:Address>urn:uuid:{DEVICE_UUID}</w:Address></w:EndpointReference>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        f"<d:Scopes>{scopes}</d:Scopes><d:XAddrs>{xaddr}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>"
        "</d:ProbeMatch></d:ProbeMatches>"
    )
    return envelope(body, relates_to=message_id)


def discovery_server() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MULTICAST[1]))
    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST[0]), socket.INADDR_ANY)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        pass  # без multicast остаются адресные запросы
    while True:
        data, addr = sock.recvfrom(65535)
        if b"Probe" not in data or b"ProbeMatches" in data:
            continue
        m = re.search(rb"MessageID[^>]*>([^<]+)<", data)
        message_id = m.group(1).decode() if m else f"uuid:{uuid.uuid4()}"
        sock.sendto(probe_match(message_id), addr)


# ---------------------------------------------------------------- SOAP


def authorized(body: bytes) -> bool:
    user = re.search(rb"<(?:\w+:)?Username>([^<]*)<", body)
    digest = re.search(rb"<(?:\w+:)?Password(?:\s[^>]*)?>([^<]*)<", body)
    nonce = re.search(rb"<(?:\w+:)?Nonce(?:\s[^>]*)?>([^<]*)<", body)
    created = re.search(rb"<(?:\w+:)?Created>([^<]*)<", body)
    if not (user and digest and nonce and created):
        return False
    if user.group(1).decode() != USER:
        return False
    expected = base64.b64encode(
        hashlib.sha1(base64.b64decode(nonce.group(1)) + created.group(1) + PASSWORD.encode()).digest()
    )
    return expected == digest.group(1).strip()


def fault_not_authorized() -> bytes:
    return envelope(
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value>"
        "</s:Subcode></s:Code><s:Reason><s:Text xml:lang=\"en\">Sender not Authorized</s:Text></s:Reason></s:Fault>"
    )


def handle(action: str, body: bytes) -> tuple[int, bytes]:
    ip = own_ip()
    if action == "GetSystemDateAndTime":
        now = datetime.now(UTC)
        return 200, envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:UTCDateTime>"
            f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )
    if not authorized(body):
        return 400, fault_not_authorized()
    if action == "GetDeviceInformation":
        return 200, envelope(
            "<tds:GetDeviceInformationResponse><tds:Manufacturer>Qoima Sim</tds:Manufacturer>"
            "<tds:Model>Camera Simulator</tds:Model><tds:FirmwareVersion>1.0</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{HOSTNAME}</tds:SerialNumber><tds:HardwareId>QSIM-1</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )
    if action == "GetCapabilities":
        return 200, envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>http://{ip}:80/onvif/device_service</tt:XAddr></tt:Device>"
            f"<tt:Media><tt:XAddr>http://{ip}:80/onvif/media_service</tt:XAddr></tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )
    if action == "GetProfiles":
        items = "".join(
            f'<trt:Profiles token="{token}" fixed="true"><tt:Name>{name}</tt:Name>'
            '<tt:VideoSourceConfiguration token="VSC_1"><tt:SourceToken>VideoSource_1</tt:SourceToken>'
            "</tt:VideoSourceConfiguration>"
            f'<tt:VideoEncoderConfiguration token="VEC_{token}"><tt:Encoding>H264</tt:Encoding>'
            f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
            "<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit></tt:RateControl>"
            "</tt:VideoEncoderConfiguration></trt:Profiles>"
            for token, name, w, h, _path in PROFILES
        )
        return 200, envelope(f"<trt:GetProfilesResponse>{items}</trt:GetProfilesResponse>")
    if action == "GetStreamUri":
        m = re.search(rb"ProfileToken>([^<]+)<", body)
        token = m.group(1).decode() if m else ""
        path = next((p for t, _n, _w, _h, p in PROFILES if t == token), None)
        if path is None:
            return 400, envelope("<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code></s:Fault>")
        return 200, envelope(
            f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://{ip}:554/{path}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>"
        )
    return 400, envelope("<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code></s:Fault>")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        m = re.search(rb"<s:Body>\s*<(?:\w+:)?(\w+)", body) or re.search(rb"Body[^>]*>\s*<(?:\w+:)?(\w+)", body)
        action = m.group(1).decode() if m else ""
        status, payload = handle(action, body)
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


if __name__ == "__main__":
    threading.Thread(target=discovery_server, daemon=True).start()
    ThreadingHTTPServer(("", 80), Handler).serve_forever()
