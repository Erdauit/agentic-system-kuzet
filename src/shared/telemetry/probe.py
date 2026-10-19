"""HTTP-проба для Docker healthcheck.

    python -m shared.telemetry.probe <url> [--insecure-localhost]

Код возврата 0 — эндпоинт ответил 2xx, иначе 1. Отдельный модуль нужен потому, что в
python:slim-образах нет curl/wget. --insecure-localhost отключает проверку TLS-сертификата
только для 127.0.0.1/localhost (самоподписанный сертификат core-api внутри своего контейнера).
"""

from __future__ import annotations

import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def probe(url: str, *, insecure_localhost: bool = False, timeout_s: float = 3.0) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("ожидается http:// или https://")
    context: ssl.SSLContext | None = None
    if parts.scheme == "https" and insecure_localhost:
        if parts.hostname not in _LOCAL_HOSTS:
            raise ValueError("--insecure-localhost разрешён только для localhost")
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout_s, context=context) as resp:  # noqa: S310 — схема проверена выше
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    insecure = "--insecure-localhost" in args
    urls = [a for a in args if not a.startswith("--")]
    if len(urls) != 1:
        print(
            "использование: python -m shared.telemetry.probe <url> [--insecure-localhost]",
            file=sys.stderr,
        )
        return 2
    return 0 if probe(urls[0], insecure_localhost=insecure) else 1


if __name__ == "__main__":
    raise SystemExit(main())
