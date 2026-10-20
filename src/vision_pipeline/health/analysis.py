"""Анализ одного кадра для Camera Health (на уменьшенной копии шириной analysis_width)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from shared.media.quality import (
    Gray,
    laplacian_variance,
    mean_luma,
    thumbnail,
    tile_sharpness,
    to_analysis_size,
)


@dataclass(frozen=True)
class FrameStats:
    at: float  # time.monotonic()
    thumb: np.ndarray  # миниатюра 160×90 для проверки «замёрз»
    luma: float
    sharpness: float
    tiles: np.ndarray  # резкость по плиткам сетки


def analyze(
    gray: Gray, at: float, analysis_width: int, grid: tuple[int, int]
) -> tuple[FrameStats, Gray]:
    small = to_analysis_size(gray, analysis_width)
    return (
        FrameStats(
            at=at,
            thumb=thumbnail(small),
            luma=mean_luma(small),
            sharpness=laplacian_variance(small),
            tiles=tile_sharpness(small, grid),
        ),
        small,
    )
