"""Учётные данные камер (SPEC.md §10): файлы в .secrets/runtime/cameras/, права 600.

Пишет и читает только core-api. Пароли не попадают в БД, логи и ответы API; vision-pipeline
их не знает вовсе — он читает потоки через MediaMTX.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from core_api.cameras.onvif import Credentials
from shared.storage.blobstore import validate_key


class CredentialStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "cameras"

    def _path(self, camera_id: str) -> Path:
        validate_key(camera_id)
        return self.root / f"{camera_id}.json"

    def save(self, camera_id: str, creds: Credentials) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.dumps({"username": creds.username, "password": creds.password}).encode()
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            Path(tmp).replace(self._path(camera_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load(self, camera_id: str) -> Credentials | None:
        path = self._path(camera_id)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Credentials(username=str(raw["username"]), password=str(raw["password"]))

    def delete(self, camera_id: str) -> None:
        self._path(camera_id).unlink(missing_ok=True)

    def has(self, camera_id: str) -> bool:
        return self._path(camera_id).is_file()
