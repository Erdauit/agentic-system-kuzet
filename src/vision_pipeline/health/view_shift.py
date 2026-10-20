"""Сдвиг ракурса: ORB-сопоставление текущего кадра со снапшотом базовой линии (SPEC.md §5.9).

Оценка 0..1:
- согласованных ORB-пар (RANSAC) ≥ view_shift_min_inliers — сцена узнана, оценка = среднее
  смещение точек кадра по найденному преобразованию / view_shift_full_displacement;
- пар меньше — сцену не узнать (сильный сдвиг, подмена ракурса) — оценка 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from shared.media.quality import Gray

# Меньше 20 точек в базовой линии — текстуры мало (тёмная сцена), оценка невозможна.
MIN_BASELINE_KEYPOINTS = 20
LOWE_RATIO = 0.75  # стандартный тест Лоу для отсева неоднозначных пар


@dataclass
class Reference:
    keypoints: Any
    descriptors: Any
    shape: tuple[int, int]


class ViewShiftDetector:
    def __init__(self, n_features: int, full_displacement: float, min_inliers: int) -> None:
        self.orb = cv2.ORB.create(nfeatures=n_features)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.full_displacement = full_displacement
        self.min_inliers = min_inliers

    def reference(self, gray: Gray) -> Reference | None:
        kp, desc = self.orb.detectAndCompute(gray, None)
        if desc is None or len(kp) < MIN_BASELINE_KEYPOINTS:
            return None
        return Reference(kp, desc, (gray.shape[0], gray.shape[1]))

    def score(self, ref: Reference, gray: Gray) -> float:
        img: Any = gray
        if gray.shape[:2] != ref.shape:
            img = cv2.resize(gray, (ref.shape[1], ref.shape[0]), interpolation=cv2.INTER_AREA)
        kp, desc = self.orb.detectAndCompute(img, None)
        if desc is None or len(kp) < 4:
            return 1.0
        pairs = self.matcher.knnMatch(ref.descriptors, desc, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < LOWE_RATIO * p[1].distance]
        if len(good) < self.min_inliers:
            return 1.0
        src = np.array([ref.keypoints[m.queryIdx].pt for m in good], dtype=np.float32).reshape(
            -1, 1, 2
        )
        dst = np.array([kp[m.trainIdx].pt for m in good], dtype=np.float32).reshape(-1, 1, 2)
        matrix, inliers = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0
        )
        if matrix is None or inliers is None:
            return 1.0
        if int(inliers.sum()) < self.min_inliers:
            return 1.0
        h, w = ref.shape
        grid = np.array(
            [[x, y] for x in np.linspace(0, w, 5) for y in np.linspace(0, h, 5)], dtype=np.float64
        )
        moved = grid @ matrix[:, :2].T + matrix[:, 2]
        displacement = float(np.linalg.norm(moved - grid, axis=1).mean()) / float(np.hypot(w, h))
        return round(min(1.0, displacement / self.full_displacement), 3)
