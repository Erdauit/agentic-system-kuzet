"""Артефакты моделей в каталоге данных: <models_dir>/<name>/manifest.yaml + model.onnx.

Манифест (shared.schemas.ModelManifest) несёт лицензию, sha256 весов и параметры запуска
(ModelRuntime). Перед загрузкой sha256 файла сверяется с манифестом: подменённый или
повреждённый файл не запускается. Нет артефакта — модель недоступна, и сервис честно сообщает
об этом в /readyz (capabilities без соответствующего сценария).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from shared.licenses import load_policy, parse
from shared.licenses.spdx import Verdict
from shared.schemas import ModelManifest, ModelRuntime


class ModelUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class ModelArtifact:
    manifest: ModelManifest
    runtime: ModelRuntime
    path: Path

    @property
    def version(self) -> str:
        return f"{self.manifest.name}-{self.manifest.version}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def load_artifact(models_dir: Path, name: str) -> ModelArtifact:
    folder = models_dir / name
    manifest_path, model_path = folder / "manifest.yaml", folder / "model.onnx"
    if not manifest_path.is_file() or not model_path.is_file():
        raise ModelUnavailableError(
            f"модели {name} нет в {folder} (нужны manifest.yaml и model.onnx)"
        )
    try:
        manifest = ModelManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
    except (ValidationError, yaml.YAMLError) as exc:
        raise ModelUnavailableError(f"{manifest_path}: некорректный манифест: {exc}") from exc
    if manifest.runtime is None:
        raise ModelUnavailableError(f"{manifest_path}: нет раздела runtime")
    policy = load_policy()
    if policy.spdx.evaluate(parse(manifest.license)) is not Verdict.ALLOWED:
        raise ModelUnavailableError(
            f"{name}: лицензия весов {manifest.license} не разрешена (SPEC.md §2.7)"
        )
    actual = sha256_file(model_path)
    if actual != manifest.weights_sha256:
        raise ModelUnavailableError(
            f"{name}: sha256 файла не совпадает с манифестом — файл подменён или повреждён"
        )
    return ModelArtifact(manifest=manifest, runtime=manifest.runtime, path=model_path)
