"""Подготовка входа модели: resize, цвет, нормализация, NCHW."""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

from shared.schemas import ModelRuntime

RGB = NDArray[np.uint8]  # кадр H×W×3, RGB


def to_tensor(image: RGB, rt: ModelRuntime) -> NDArray[np.float32]:
    """Кадр RGB → (3, H, W) float32 по параметрам модели.

    cv2.INTER_LINEAR — билинейная интерполяция с центрами пикселей (half-pixel) без антиалиасинга,
    как у RF-DETR predict() (torchvision resize, antialias=False).
    """
    resized = cv2.resize(image, (rt.input_width, rt.input_height), interpolation=cv2.INTER_LINEAR)
    if rt.color == "bgr":
        resized = resized[:, :, ::-1]
    x = np.asarray(resized, dtype=np.float32) / np.float32(rt.scale)
    mean = np.asarray(rt.mean, dtype=np.float32)
    std = np.asarray(rt.std, dtype=np.float32)
    normalized = np.asarray((x - mean) / std, dtype=np.float32)
    return np.ascontiguousarray(normalized.transpose(2, 0, 1), dtype=np.float32)
