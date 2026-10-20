"""Геометрия кадра: гомография плоскости пола, точки в полигоне (SPEC.md §3.2 шаг 5, §5.8).

Только numpy: гомографию считает и core-api (при сохранении калибровки), и vision-pipeline.
Координаты кадра — нормализованные (0..1), как в контрактах зон.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

Matrix = NDArray[np.float64]
XY = tuple[float, float]


class CalibrationError(ValueError):
    pass


def homography(src: Sequence[XY], dst: Sequence[XY]) -> Matrix:
    """Матрица 3×3, переводящая 4 точки src в 4 точки dst (DLT, нормированная h33 = 1)."""
    if len(src) != 4 or len(dst) != 4:
        raise CalibrationError("нужно ровно 4 пары точек")
    rows = []
    for (x, y), (u, v) in zip(src, dst, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    a = np.asarray(rows, dtype=np.float64)
    _, s, vt = np.linalg.svd(a)
    if s[-2] < 1e-9:
        raise CalibrationError("точки вырождены (три на одной прямой?)")
    h = vt[-1].reshape(3, 3)
    if abs(h[2, 2]) < 1e-12:
        raise CalibrationError("гомография вырождена")
    result: Matrix = h / h[2, 2]
    return result


def project(h: Matrix, point: XY) -> XY:
    x, y = point
    v = h @ np.array([x, y, 1.0])
    if abs(v[2]) < 1e-12:
        raise CalibrationError("точка уходит в бесконечность (за горизонтом)")
    return float(v[0] / v[2]), float(v[1] / v[2])


def floor_homography(
    image_points: Sequence[XY], known: tuple[int, int, float] | None = None
) -> tuple[Matrix, bool]:
    """Гомография «кадр → пол» по 4 точкам на полу.

    Без известного расстояния точки считаются углами квадрата 1×1 (единицы условные): этого
    достаточно, чтобы нормировать движения на рост человека и найти точку опоры. С известным
    расстоянием (индексы двух точек и метры) масштаб — в метрах. Возвращает (H, в_метрах).
    """
    unit = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    h = homography(image_points, unit)
    if known is None:
        return h, False
    a, b, meters = known
    pa, pb = project(h, image_points[a]), project(h, image_points[b])
    dist = math.dist(pa, pb)
    if dist <= 0:
        raise CalibrationError("точки известного расстояния совпадают на плоскости")
    scale = np.diag([meters / dist, meters / dist, 1.0])
    scaled: Matrix = scale @ h
    return scaled, True


def point_in_polygon(point: XY, polygon: Sequence[XY]) -> bool:
    """Чётность пересечений луча (точка на границе считается внутри для одной из сторон)."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside
