"""Приёмник алертов для разработки и e2e (`qoima sim up`): подменяет Telegram Bot API и HTTP-реле.

    POST /bot<токен>/sendMessage|sendPhoto|sendVideo  — как Telegram (multipart или JSON)
    POST /relay                                        — HTTP-реле/сирена
    GET  /_records                                     — что пришло (JSON)
    POST /_reset                                       — очистить
    POST /_telegram?down=1|0                           — «Telegram недоступен» (HTTP 502)

Только стандартная библиотека. Не для объекта: токены и сообщения хранятся в памяти и
отдаются любому, кто спросит.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

RECORDS: list[dict[str, object]] = []
STATE = {"telegram_down": False}
LOCK = threading.Lock()
NAME = re.compile(rb'name="([^"]+)"')


def _multipart(body: bytes, content_type: str) -> dict[str, bytes]:
    """multipart/form-data: части по границе, у каждой — заголовки до пустой строки и значение."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if m is None:
        return {}
    fields: dict[str, bytes] = {}
    for part in body.split(b"--" + m.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        name = NAME.search(head)
        if name:
            fields[name.group(1).decode()] = value.removesuffix(b"\r\n")
    return fields


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # тише в логах контейнера
        return

    def _json(self, code: int, data: object) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/_records"):
            with LOCK:
                self._json(200, list(RECORDS))
        elif self.path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"ok": False})

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if url.path == "/_reset":
            with LOCK:
                RECORDS.clear()
            self._json(200, {"ok": True})
            return
        if url.path == "/_telegram":
            STATE["telegram_down"] = parse_qs(url.query).get("down", ["0"])[0] == "1"
            self._json(200, {"ok": True, **STATE})
            return
        if url.path == "/relay":
            with LOCK:
                RECORDS.append({"kind": "relay", "at": time.time(), "body": json.loads(body or b"{}")})
            self._json(200, {"ok": True})
            return
        m = re.fullmatch(r"/bot[^/]+/(send\w+)", url.path)
        if m is None:
            self._json(404, {"ok": False, "description": "Not Found"})
            return
        if STATE["telegram_down"]:
            self._json(502, {"ok": False, "description": "Bad Gateway (симуляция недоступности)"})
            return
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            fields = {k: str(v).encode() for k, v in json.loads(body or b"{}").items()}
        else:
            fields = _multipart(body, ctype)
        record = {
            "kind": "telegram",
            "at": time.time(),
            "method": m.group(1),
            "chat_id": fields.get("chat_id", b"").decode(),
            "text": (fields.get("caption") or fields.get("text") or b"").decode(errors="replace"),
            "file_bytes": len(fields.get("photo") or fields.get("video") or b""),
        }
        with LOCK:
            RECORDS.append(record)
        self._json(200, {"ok": True, "result": {"message_id": len(RECORDS)}})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104 — внутри контейнера
