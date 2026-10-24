"""Подготовка детектора людей RF-DETR для поставки (выполняет инженер Qoima, не объект).

    uv run --with "rfdetr==1.11.2" --with "onnx>=1.17" python tools/models/prepare_rfdetr.py \
        --size nano --out /var/lib/qoima/models/registry

Скачивает предобученные на COCO веса RF-DETR (Apache-2.0, Roboflow), выгружает ONNX с
динамическим батчем и пишет manifest.yaml с sha256 и параметрами запуска (ModelRuntime).
На объекте torch не нужен: hot path запускает только ONNX (ONNX Runtime / TensorRT EP).

Параметры запуска взяты из исходников rfdetr 1.11: вход `input` NCHW float32 RGB,
ImageNet-нормализация; выходы `dets` (cxcywh, нормализованные) и `labels` (логиты 91 класса
разреженной COCO-разметки, person = 1). Лицензии данных обучения (COCO) — docs/DATA_LICENSES.md.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import yaml

SIZES = {
    "nano": ("RFDETRNano", 384),
    "small": ("RFDETRSmall", 512),
    "medium": ("RFDETRMedium", 576),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=sorted(SIZES), default="nano")
    parser.add_argument(
        "--out", type=Path, required=True, help="каталог моделей (inference.models_dir)"
    )
    parser.add_argument("--name", default="rfdetr-person")
    args = parser.parse_args()

    import rfdetr  # тяжёлые зависимости только здесь

    package_version = metadata.version("rfdetr")  # rfdetr 1.11.2 не экспортирует __version__
    cls_name, resolution = SIZES[args.size]
    model = getattr(rfdetr, cls_name)()
    work = args.out / f".export-{args.name}"
    work.mkdir(parents=True, exist_ok=True)
    exported = Path(
        model.export(output_dir=str(work), dynamic_batch=True, format="onnx", verbose=False)
    )
    onnx_path = exported if exported.suffix == ".onnx" else next(exported.rglob("*.onnx"))
    target = args.out / args.name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, target / "model.onnx")
    shutil.rmtree(work, ignore_errors=True)
    digest = hashlib.sha256((target / "model.onnx").read_bytes()).hexdigest()
    manifest = {
        "name": args.name,
        "version": f"{package_version}+{args.size}",
        "task_type": "detection",
        "framework": "rf-detr",
        "license": "Apache-2.0",
        "weights_sha256": digest,
        "source": "pretrained",
        "metrics": {},
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "format": "onnx",
            "input_name": "input",
            "input_width": resolution,
            "input_height": resolution,
            "color": "rgb",
            "mean": [0.485, 0.456, 0.406],
            "std": [0.229, 0.224, 0.225],
            "scale": 255.0,
            "outputs": ["dets", "labels"],
            "dynamic_batch": True,
            "decoder": "rfdetr",
            "class_ids": {"person": 1},
        },
    }
    (target / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    )
    print(f"{target}: model.onnx sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
