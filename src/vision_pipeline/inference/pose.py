"""Поза top-down в формате RTMPose SimCC (Apache-2.0).

Схема (как в официальной реализации RTMPose/rtmlib): бокс человека расширяется в crop_padding
раз и приводится к пропорциям входа модели → аффинное преобразование кропа → модель выдаёт
simcc_x (K, W·r) и simcc_y (K, H·r) → координата = argmax / r, уверенность = (max_x + max_y) / 2
(как get_simcc_maximum в rtmlib 0.0.16: пороги keypoint_min откалиброваны под среднее)
→ обратное преобразование в кадр. Выбор между RTMPose и RF-DETR keypoints — по бенчмарку на
клипах объекта (docs/TODO.md); до проверки на реальной модели декодер покрыт синтетическими тестами.
"""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

from vision_pipeline.inference.preprocess import RGB
from vision_pipeline.inference.runner import OnnxModel
from vision_pipeline.tracking.tracker import Box


def crop_transform(
    box: Box, frame_w: int, frame_h: int, in_w: int, in_h: int, padding: float
) -> NDArray[np.float64]:
    """Аффинная матрица 2×3 «кадр (пиксели) → вход модели» для бокса в нормализованных координатах."""
    x1, y1, x2, y2 = box[0] * frame_w, box[1] * frame_h, box[2] * frame_w, box[3] * frame_h
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w, h = (x2 - x1) * padding, (y2 - y1) * padding
    aspect = in_w / in_h
    if w > h * aspect:
        h = w / aspect
    else:
        w = h * aspect
    sx, sy = in_w / w, in_h / h
    return np.array([[sx, 0, in_w / 2 - cx * sx], [0, sy, in_h / 2 - cy * sy]], dtype=np.float64)


def decode_simcc(
    simcc_x: NDArray[np.float32], simcc_y: NDArray[np.float32], split_ratio: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """(K, Wr), (K, Hr) → координаты во входе модели (K, 2) и уверенности (K,)."""
    xi, yi = simcc_x.argmax(axis=1), simcc_y.argmax(axis=1)
    conf = ((simcc_x.max(axis=1) + simcc_y.max(axis=1)) / 2).astype(np.float64)
    locs = np.stack([xi, yi], axis=1).astype(np.float64) / split_ratio
    conf[conf <= 0] = 0.0
    return locs, conf


class PoseEstimator:
    def __init__(self, model: OnnxModel) -> None:
        rt = model.artifact.runtime
        if rt.decoder != "rtmpose_simcc" or rt.simcc_split_ratio is None or rt.crop_padding is None:
            raise ValueError(
                "модель позы должна быть в формате rtmpose_simcc (split ratio, padding)"
            )
        self.model = model
        self.rt = rt

    @property
    def version(self) -> str:
        return self.model.artifact.version

    def estimate(self, frame: RGB, boxes: list[Box]) -> list[NDArray[np.float64]]:
        """Ключевые точки (K, 3) в нормализованных координатах кадра для каждого бокса."""
        if not boxes:
            return []
        h, w = frame.shape[:2]
        rt = self.rt
        mats, crops = [], []
        for box in boxes:
            m = crop_transform(box, w, h, rt.input_width, rt.input_height, rt.crop_padding or 1.25)
            crop = cv2.warpAffine(
                frame, m, (rt.input_width, rt.input_height), flags=cv2.INTER_LINEAR
            )
            if rt.color == "bgr":
                crop = crop[:, :, ::-1]
            x = (
                crop.astype(np.float32) / np.float32(rt.scale) - np.asarray(rt.mean, np.float32)
            ) / np.asarray(rt.std, np.float32)
            crops.append(np.ascontiguousarray(x.transpose(2, 0, 1)))
            mats.append(m)
        out = self.model.run(np.stack(crops))
        sx_name, sy_name = rt.outputs[0], rt.outputs[1]
        result = []
        for i, m in enumerate(mats):
            locs, conf = decode_simcc(out[sx_name][i], out[sy_name][i], rt.simcc_split_ratio or 2.0)
            inv = cv2.invertAffineTransform(m)
            pts = locs @ inv[:, :2].T + inv[:, 2]
            kp = np.column_stack([pts[:, 0] / w, pts[:, 1] / h, conf])
            result.append(kp)
        return result
