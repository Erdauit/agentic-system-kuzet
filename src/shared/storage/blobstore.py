"""BlobStore — хранилище файлов (клипы, кадры, датасеты, модели), SPEC.md §4.

Интерфейс повторяет семантику S3 (ключ → объект, листинг по префиксу), чтобы при необходимости
подключить внешнее S3-совместимое хранилище без изменения вызывающего кода. Бэкенд по умолчанию —
локальная ФС (`LocalFSBlobStore`); MinIO не используется (лицензия AGPL, SPEC.md §2.7).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

# Ключ: сегменты из латиницы, цифр, '.', '_', '-', разделённые '/'. Без '..', без ведущего '/'.
_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,199}$")
_CHUNK = 1024 * 1024


class BlobNotFoundError(KeyError):
    pass


class InvalidBlobKeyError(ValueError):
    pass


@dataclass(frozen=True)
class BlobInfo:
    key: str
    size: int
    sha256: str | None
    modified_at: datetime


def validate_key(key: str) -> str:
    if not key or key.startswith("/") or key.endswith("/"):
        raise InvalidBlobKeyError(f"некорректный ключ: {key!r}")
    segments = key.split("/")
    for seg in segments:
        if seg in {".", ".."} or not _KEY_SEGMENT.match(seg):
            raise InvalidBlobKeyError(f"некорректный сегмент ключа {seg!r} в {key!r}")
    return key


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> BlobInfo: ...
    def put_file(self, key: str, source: Path) -> BlobInfo: ...
    def get(self, key: str) -> bytes: ...
    def open(self, key: str) -> BinaryIO: ...
    def stat(self, key: str) -> BlobInfo: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str = "") -> Iterator[BlobInfo]: ...


class LocalFSBlobStore:
    """Бэкенд на локальной ФС. Запись атомарна: временный файл в том же каталоге + rename,
    поэтому читатель никогда не увидит недописанный клип."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):  # защита от обхода через симлинки
            raise InvalidBlobKeyError(f"ключ {key!r} выходит за пределы хранилища")
        return path

    def _write_atomic(self, path: Path, chunks: Iterator[bytes]) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in chunks:
                    digest.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def put(self, key: str, data: bytes) -> BlobInfo:
        path = self._path(key)
        sha = self._write_atomic(path, iter([data]))
        return self._info(key, path, sha)

    def put_file(self, key: str, source: Path) -> BlobInfo:
        path = self._path(key)

        def chunks() -> Iterator[bytes]:
            with source.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk

        sha = self._write_atomic(path, chunks())
        return self._info(key, path, sha)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(key) from exc

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise BlobNotFoundError(key) from exc

    def stat(self, key: str) -> BlobInfo:
        path = self._path(key)
        if not path.is_file():
            raise BlobNotFoundError(key)
        return self._info(key, path, None)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(key) from exc

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                yield self._info(key, path, None)

    @staticmethod
    def _info(key: str, path: Path, sha: str | None) -> BlobInfo:
        st = path.stat()
        return BlobInfo(
            key=key,
            size=st.st_size,
            sha256=sha,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=UTC),
        )
