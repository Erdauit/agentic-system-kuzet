"""Хранилища: BlobStore (файлы), Postgres, Valkey, секреты.

Клиенты Postgres и Valkey импортируются из подмодулей напрямую (shared.storage.postgres,
shared.storage.valkey): их зависимости ставятся через extras и есть не в каждом сервисе.
Клиент Qdrant появится в Фазе 6 (docs/TODO.md).
"""

from shared.storage.blobstore import (
    BlobInfo,
    BlobNotFoundError,
    BlobStore,
    InvalidBlobKeyError,
    LocalFSBlobStore,
    validate_key,
)
from shared.storage.secrets import SecretNotFoundError, read_secret

__all__ = [
    "BlobInfo",
    "BlobNotFoundError",
    "BlobStore",
    "InvalidBlobKeyError",
    "LocalFSBlobStore",
    "SecretNotFoundError",
    "read_secret",
    "validate_key",
]
