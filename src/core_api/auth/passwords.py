"""Хеширование паролей scrypt (stdlib hashlib) — без внешних зависимостей.

Параметры n=2^15, r=8, p=1: ~50–100 мс на вход на серверном CPU и ~32 МБ памяти на попытку —
дорого для перебора, незаметно для человека.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P, _LEN = 2**15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_LEN, maxmem=_MAXMEM
    )
    return "$".join(
        [
            "scrypt",
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = base64.b64decode(digest_b64)
    actual = hashlib.scrypt(
        password.encode(),
        salt=base64.b64decode(salt_b64),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(expected),
        maxmem=_MAXMEM,
    )
    return hmac.compare_digest(actual, expected)


def generate_password(length: int = 16) -> str:
    """Временный пароль: без похожих символов (0/O, 1/l), чтобы его можно было продиктовать."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
