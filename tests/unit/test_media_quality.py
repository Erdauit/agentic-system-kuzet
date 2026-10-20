"""Метрики качества кадра (shared.media.quality) на синтетических кадрах."""

from __future__ import annotations

import numpy as np
import pytest

from shared.media.quality import (
    laplacian_variance,
    mean_abs_diff,
    mean_luma,
    noise_sigma,
    occlusion_ratio,
    resize_area,
    thumbnail,
    tile_sharpness,
    to_analysis_size,
)

RNG = np.random.default_rng(42)


def textured(h: int = 180, w: int = 320) -> np.ndarray:
    return RNG.integers(0, 256, size=(h, w), dtype=np.uint8)


def box_blur(img: np.ndarray, k: int = 7) -> np.ndarray:
    f = img.astype(np.float64)
    out = np.zeros_like(f)
    pad = np.pad(f, k // 2, mode="edge")
    for dy in range(k):
        for dx in range(k):
            out += pad[dy : dy + f.shape[0], dx : dx + f.shape[1]]
    return np.clip(out / (k * k), 0, 255).astype(np.uint8)


def test_blur_drops_sharpness() -> None:
    img = textured()
    assert laplacian_variance(box_blur(img)) < 0.1 * laplacian_variance(img)


def test_luma_and_noise() -> None:
    flat = np.full((90, 160), 16, dtype=np.uint8)
    assert mean_luma(flat) == 16
    assert noise_sigma(flat) == pytest.approx(0.0)
    noisy = np.clip(128 + RNG.normal(0, 5, size=(180, 320)), 0, 255).astype(np.uint8)
    assert 3.5 < noise_sigma(noisy) < 6.5


def test_thumbnail_mad_separates_frozen_from_live() -> None:
    a = textured()
    assert mean_abs_diff(thumbnail(a), thumbnail(a.copy())) == 0.0
    shifted = np.roll(a, 3, axis=1)
    assert mean_abs_diff(thumbnail(a), thumbnail(shifted)) > 1.0


def test_occlusion_ratio() -> None:
    base = textured()
    covered = base.copy()
    covered[:, : int(320 * 0.85)] = 128
    t_base = tile_sharpness(base, (8, 6))
    assert occlusion_ratio(tile_sharpness(base, (8, 6)), t_base, 0.15) == 0.0
    assert occlusion_ratio(tile_sharpness(covered, (8, 6)), t_base, 0.15) >= 0.75
    flat = np.full((180, 320), 100, dtype=np.uint8)
    assert occlusion_ratio(tile_sharpness(flat, (8, 6)), tile_sharpness(flat, (8, 6)), 0.15) is None


def test_resizing() -> None:
    img = textured(720, 1280)
    assert to_analysis_size(img, 320).shape == (180, 320)
    assert resize_area(np.full((10, 10), 7, dtype=np.uint8), 5, 5).mean() == 7
    with pytest.raises(ValueError):
        resize_area(img, 2000, 10)
    with pytest.raises(ValueError):
        mean_luma(np.zeros((4, 4), dtype=np.uint8))
