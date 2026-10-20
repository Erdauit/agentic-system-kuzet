"""Метрики качества кадра (SPEC.md §5.9). Вход — кадр в оттенках серого, uint8, форма (H, W).

Все функции детерминированы и не зависят от OpenCV: их используют и core-api (базовая линия при
добавлении камеры), и vision-pipeline (Camera Health). Требует extra `qoima-shared[media]`.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

Gray = NDArray[np.uint8]
FloatImage = NDArray[np.float64]

# Ядро оценки шума Immerkær (1996): отклик на гладкие области ≈ 0, остаётся шум сенсора.
_NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)


def _check(gray: Gray) -> None:
    if gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError("ожидается серый кадр uint8 формы (H, W)")
    if gray.shape[0] < 9 or gray.shape[1] < 9:
        raise ValueError("кадр слишком мал для анализа")


def mean_luma(gray: Gray) -> float:
    _check(gray)
    return float(gray.mean())


def laplacian(gray: Gray) -> FloatImage:
    """4-связный лапласиан по внутренней области кадра."""
    g = gray.astype(np.float64)
    return g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4.0 * g[1:-1, 1:-1]


def laplacian_variance(gray: Gray) -> float:
    """Резкость: дисперсия лапласиана. Падает при расфокусе, запотевании, заслонении."""
    _check(gray)
    return float(laplacian(gray).var())


def noise_sigma(gray: Gray) -> float:
    """Оценка σ шума (Immerkær): быстрая и устойчивая к содержимому сцены."""
    _check(gray)
    g = gray.astype(np.float64)
    h, w = g.shape
    conv = np.zeros((h - 2, w - 2))
    for dy in range(3):
        for dx in range(3):
            conv += _NOISE_KERNEL[dy, dx] * g[dy : dy + h - 2, dx : dx + w - 2]
    return float(math.sqrt(math.pi / 2.0) * np.abs(conv).sum() / (6.0 * (w - 2) * (h - 2)))


def resize_area(gray: Gray, width: int, height: int) -> FloatImage:
    """Уменьшение усреднением по областям (без интерполяции и внешних библиотек)."""
    h, w = gray.shape
    if width > w or height > h:
        raise ValueError("resize_area только уменьшает кадр")
    rows = np.linspace(0, h, height + 1).astype(int)
    cols = np.linspace(0, w, width + 1).astype(int)
    sums = np.add.reduceat(
        np.add.reduceat(gray.astype(np.float64), rows[:-1], axis=0), cols[:-1], axis=1
    )
    counts = np.outer(np.diff(rows), np.diff(cols))
    result: FloatImage = sums / counts
    return result


def thumbnail(gray: Gray, width: int = 160, height: int = 90) -> Gray:
    """Миниатюра для сравнения кадров (размер детектора движения SPEC.md §5.4)."""
    return np.clip(np.rint(resize_area(gray, width, height)), 0, 255).astype(np.uint8)


def mean_abs_diff(a: Gray, b: Gray) -> float:
    """Средняя абсолютная разница двух кадров одного размера (уровни яркости)."""
    if a.shape != b.shape:
        raise ValueError("кадры разного размера")
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def tile_sharpness(gray: Gray, grid: tuple[int, int]) -> FloatImage:
    """Резкость по плиткам сетки (cols, rows) — для оценки заслонения части кадра."""
    _check(gray)
    cols, rows = grid
    lap = laplacian(gray) ** 2
    h, w = lap.shape
    r_edges = np.linspace(0, h, rows + 1).astype(int)
    c_edges = np.linspace(0, w, cols + 1).astype(int)
    sums = np.add.reduceat(np.add.reduceat(lap, r_edges[:-1], axis=0), c_edges[:-1], axis=1)
    counts = np.outer(np.diff(r_edges), np.diff(c_edges))
    result: FloatImage = sums / counts
    return result


# Плитки базовой линии почти без текстуры (гладкая стена, небо) в оценке заслонения не участвуют:
# по ним нельзя отличить «закрыто» от «так и было». Порог — средний квадрат лапласиана.
MIN_BASELINE_TILE_TEXTURE = 4.0


def occlusion_ratio(
    tiles: FloatImage, baseline_tiles: FloatImage, texture_ratio: float
) -> float | None:
    """Доля текстурных плиток базовой линии, где текстура упала ниже texture_ratio от базовой.

    None — в базовой линии нет текстурных плиток, оценка невозможна.
    """
    if tiles.shape != baseline_tiles.shape:
        raise ValueError("сетки плиток не совпадают")
    textured = baseline_tiles > MIN_BASELINE_TILE_TEXTURE
    if not textured.any():
        return None
    lost = tiles[textured] < texture_ratio * baseline_tiles[textured]
    return float(lost.mean())


def to_analysis_size(gray: Gray, width: int) -> Gray:
    """Кадр для анализа фиксированной ширины (высота — с сохранением пропорций)."""
    h, w = gray.shape
    if w <= width:
        return gray
    height = max(9, round(h * width / w))
    return np.clip(np.rint(resize_area(gray, width, height)), 0, 255).astype(np.uint8)
