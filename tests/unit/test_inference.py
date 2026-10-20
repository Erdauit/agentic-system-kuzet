"""Инференс на тестовых ONNX-моделях с сигнатурами RF-DETR и RTMPose (настоящих весов нет).

Модели собираются здесь же через onnx.helper: выходы — константы, размноженные по батчу.
Проверяется реальный путь: манифест и sha256 → ONNX Runtime → декодеры → нормализованные
координаты кадра; батчер собирает запросы разных камер.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import onnx
import pytest
import yaml
from onnx import TensorProto, helper, numpy_helper

from vision_pipeline.inference.batcher import Batcher
from vision_pipeline.inference.detector import PersonDetector
from vision_pipeline.inference.pose import PoseEstimator, crop_transform, decode_simcc
from vision_pipeline.inference.registry import ModelUnavailableError, load_artifact, sha256_file
from vision_pipeline.inference.runner import OnnxModel


def const_model(
    path: Path, input_shape: tuple[int, int, int], outputs: dict[str, np.ndarray]
) -> None:
    """Граф: выход_i = Tile(const_i[None], [N, 1, …]), N — батч входа."""
    c, h, w = input_shape
    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, ["N", c, h, w])
    nodes = [
        helper.make_node("Shape", ["input"], ["shape"]),
        helper.make_node("Slice", ["shape", "zero", "one"], ["batch"]),
    ]
    inits = [
        numpy_helper.from_array(np.array([0], np.int64), "zero"),
        numpy_helper.from_array(np.array([1], np.int64), "one"),
    ]
    outs = []
    for name, value in outputs.items():
        ones = np.ones(value.ndim, np.int64)  # повторы по осям константы, кроме батча
        inits += [
            numpy_helper.from_array(value[None].astype(np.float32), f"{name}_c"),
            numpy_helper.from_array(ones, f"{name}_ones"),
        ]
        nodes += [
            helper.make_node("Concat", ["batch", f"{name}_ones"], [f"{name}_rep"], axis=0),
            helper.make_node("Tile", [f"{name}_c", f"{name}_rep"], [name]),
        ]
        outs.append(helper.make_tensor_value_info(name, TensorProto.FLOAT, ["N", *value.shape]))
    graph = helper.make_graph(nodes, "fixture", [inp], outs, initializer=inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.save(model, str(path))


def write_manifest(
    folder: Path, name: str, runtime: dict[str, object], license_: str = "Apache-2.0"
) -> None:
    manifest = {
        "name": name,
        "version": "1.0.0",
        "task_type": "detection",
        "framework": "onnx-fixture",
        "license": license_,
        "weights_sha256": sha256_file(folder / "model.onnx"),
        "source": "pretrained",
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
        "runtime": runtime,
    }
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True))


RFDETR_RT = {
    "format": "onnx",
    "input_name": "input",
    "input_width": 64,
    "input_height": 64,
    "color": "rgb",
    "mean": [0.485, 0.456, 0.406],
    "std": [0.229, 0.224, 0.225],
    "scale": 255.0,
    "outputs": ["dets", "labels"],
    "dynamic_batch": True,
    "decoder": "rfdetr",
    "class_ids": {"person": 1},
}


@pytest.fixture
def detector_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "rfdetr-person"
    folder.mkdir()
    dets = np.array([[0.5, 0.5, 0.2, 0.6], [0.2, 0.2, 0.1, 0.1], [0.8, 0.5, 0.1, 0.3]], np.float32)
    logits = np.full((3, 91), -9.0, np.float32)
    logits[0, 1] = 4.0  # человек, уверенно
    logits[1, 3] = 4.0  # машина — не человек
    logits[2, 1] = -1.0  # человек, но слабо (sigmoid ≈ 0.27)
    const_model(folder / "model.onnx", (3, 64, 64), {"dets": dets, "labels": logits})
    write_manifest(folder, "rfdetr-person", RFDETR_RT)
    return tmp_path


def test_detector_end_to_end(detector_dir: Path) -> None:
    art = load_artifact(detector_dir, "rfdetr-person")
    model = OnnxModel(art, "onnxruntime", threads=1)
    det = PersonDetector(model, "person", min_score=0.1)
    frames = [np.zeros((120, 160, 3), np.uint8) for _ in range(3)]
    result = det.detect(frames)
    assert len(result) == 3
    boxes = result[0]
    assert [round(d.score, 2) for d in boxes] == [
        0.98,
        0.27,
    ]  # машина отброшена, порядок по уверенности
    assert boxes[0].box == pytest.approx((0.4, 0.2, 0.6, 0.8))
    strict = PersonDetector(model, "person", min_score=0.5)
    assert len(strict.detect(frames[:1])[0]) == 1


def test_registry_rejects_tampered_and_unlicensed(detector_dir: Path) -> None:
    folder = detector_dir / "rfdetr-person"
    with (folder / "model.onnx").open("ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(ModelUnavailableError, match="sha256"):
        load_artifact(detector_dir, "rfdetr-person")
    write_manifest(folder, "rfdetr-person", RFDETR_RT, license_="AGPL-3.0-only")
    with pytest.raises(ModelUnavailableError, match="лицензия"):
        load_artifact(detector_dir, "rfdetr-person")
    with pytest.raises(ModelUnavailableError, match="нет"):
        load_artifact(detector_dir, "qoima-smoke-fire")


def test_pose_simcc_round_trip(tmp_path: Path) -> None:
    in_w, in_h, k, r = 48, 64, 17, 2.0
    # Пики SimCC в центре входа модели — после обратного преобразования это центр бокса.
    sx = np.zeros((k, int(in_w * r)), np.float32)
    sy = np.zeros((k, int(in_h * r)), np.float32)
    sx[:, int(in_w / 2 * r)] = 0.9
    sy[:, int(in_h / 2 * r)] = 0.8
    folder = tmp_path / "rtmpose-body"
    folder.mkdir()
    const_model(folder / "model.onnx", (3, in_h, in_w), {"simcc_x": sx, "simcc_y": sy})
    rt = {
        **RFDETR_RT,
        "input_width": in_w,
        "input_height": in_h,
        "outputs": ["simcc_x", "simcc_y"],
        "decoder": "rtmpose_simcc",
        "class_ids": {},
        "keypoints": k,
        "simcc_split_ratio": r,
        "crop_padding": 1.25,
    }
    write_manifest(folder, "rtmpose-body", rt)
    pose = PoseEstimator(
        OnnxModel(load_artifact(tmp_path, "rtmpose-body"), "onnxruntime", threads=1)
    )
    frame = np.zeros((360, 640, 3), np.uint8)
    kps = pose.estimate(frame, [(0.4, 0.2, 0.5, 0.8), (0.1, 0.1, 0.2, 0.3)])
    assert len(kps) == 2 and kps[0].shape == (k, 3)
    assert kps[0][0, 0] == pytest.approx(0.45, abs=0.01) and kps[0][0, 1] == pytest.approx(
        0.5, abs=0.01
    )
    assert kps[0][0, 2] == pytest.approx(0.85)  # среднее max_x и max_y, как в rtmlib
    assert kps[1][0, 0] == pytest.approx(0.15, abs=0.01)


def test_crop_transform_and_decode() -> None:
    m = crop_transform((0.25, 0.25, 0.75, 0.75), 100, 100, 50, 50, 1.0)
    assert m @ np.array([50, 50, 1]) == pytest.approx([25, 25])
    locs, conf = decode_simcc(
        np.eye(3, 6, 2, dtype=np.float32), np.eye(3, 6, 1, dtype=np.float32), 2.0
    )
    assert locs[0].tolist() == [1.0, 0.5] and conf.tolist() == [1.0, 1.0, 1.0]


def test_batcher_groups_cameras_within_delay() -> None:
    sizes: list[int] = []

    def fn(items: list[int]) -> list[int]:
        return [i * 10 for i in items]

    b = Batcher(fn, max_batch=8, max_delay_ms=20, on_batch=lambda n, _dt: sizes.append(n))
    futures = []
    threads = [threading.Thread(target=lambda i=i: futures.append(b.submit(i))) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(f.result(timeout=1) for f in futures) == [0, 10, 20, 30, 40]
    assert sum(sizes) == 5 and max(sizes) > 1  # запросы разных «камер» объединены
    started = time.monotonic()
    assert b.submit(7).result(timeout=1) == 70
    assert time.monotonic() - started < 0.2  # одиночный запрос ждёт не дольше max_delay
    b.close()


def test_batcher_propagates_errors() -> None:
    def boom(items: list[int]) -> list[int]:
        raise RuntimeError("модель упала")

    b = Batcher(boom, max_batch=4, max_delay_ms=5)
    with pytest.raises(RuntimeError, match="модель упала"):
        b.submit(1).result(timeout=1)
    b.close()
