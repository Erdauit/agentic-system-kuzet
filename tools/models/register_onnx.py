"""Регистрация готового ONNX-артефакта по шаблону манифеста из models/registry/.

    uv run python tools/models/register_onnx.py --template models/registry/rtmpose-body.yaml \
        --onnx end2end.onnx --out /var/lib/qoima/models/registry

Копирует модель в <out>/<name>/model.onnx, считает sha256, проставляет created_at, проверяет
манифест контрактом ModelManifest, лицензию — политикой, а имена входа/выходов — по самому ONNX.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import onnx
import yaml

from shared.licenses import load_policy, parse
from shared.licenses.spdx import Verdict
from shared.schemas import ModelManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    data = yaml.safe_load(args.template.read_text())
    data["weights_sha256"] = hashlib.sha256(args.onnx.read_bytes()).hexdigest()
    data["created_at"] = datetime.now(UTC).isoformat()
    manifest = ModelManifest.model_validate(data)
    if load_policy().spdx.evaluate(parse(manifest.license)) is not Verdict.ALLOWED:
        raise SystemExit(f"лицензия весов {manifest.license!r} не разрешена политикой")
    rt = manifest.runtime
    if rt is None:
        raise SystemExit("в шаблоне нет раздела runtime")
    graph = onnx.load(str(args.onnx), load_external_data=False).graph
    inputs = {i.name for i in graph.input}
    outputs = {o.name for o in graph.output}
    if rt.input_name not in inputs or not set(rt.outputs) <= outputs:
        raise SystemExit(f"имена не совпадают с моделью: входы {inputs}, выходы {outputs}")
    target = args.out / manifest.name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.onnx, target / "model.onnx")
    (target / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    )
    print(f"{target}: {manifest.name} {manifest.version} sha256={manifest.weights_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
