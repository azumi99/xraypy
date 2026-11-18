"""Preprocess: bg modes, resize modes, tensor shape."""

from __future__ import annotations

import numpy as np
import pytest

from app.embedding.preprocess import (
    composite_background,
    preprocess,
    resize_rgb,
)


def make_bgra(center=(90, 90, 90), border=(255, 255, 255)) -> np.ndarray:
    img = np.zeros((40, 60, 4), dtype=np.uint8)
    img[:, :, :3] = border
    img[10:30, 20:40, :3] = center
    img[:, :, 3] = 0
    img[10:30, 20:40, 3] = 255  # opaque parcel in the middle
    return img


def test_output_shape_chw():
    out = preprocess(make_bgra())
    assert out.shape == (3, 224, 224)
    assert out.dtype == np.float32


def test_bg_black_zeroes_outside():
    rgb = composite_background(make_bgra(), "black")
    assert rgb[0, 0].tolist() == [0, 0, 0]  # outside contour -> black
    assert rgb[20, 30].tolist() == [90, 90, 90]  # inside -> content kept


def test_bg_keep_ignores_alpha():
    rgb = composite_background(make_bgra(), "keep")
    assert rgb[0, 0].tolist() == [255, 255, 255]  # background untouched


def test_bg_mean_blends_with_visible_mean():
    rgb = composite_background(make_bgra(), "mean")
    assert rgb[0, 0].tolist() == [90, 90, 90]  # replaced by the visible mean
    assert rgb[20, 30].tolist() == [90, 90, 90]


def test_resize_modes():
    img = np.zeros((40, 60, 3), dtype=np.uint8)
    fill = resize_rgb(img, 100, "fill")
    assert fill.shape == (100, 100, 3)
    contain = resize_rgb(img, 100, "contain")
    assert contain.shape == (100, 100, 3)
    cover = resize_rgb(img, 100, "cover")
    assert cover.shape == (100, 100, 3)


def test_rejects_non_bgra():
    with pytest.raises(ValueError):
        composite_background(np.zeros((10, 10, 3), dtype=np.uint8), "black")


def test_normalization_roughly_standard():
    out = preprocess(make_bgra(center=(128, 128, 128)))
    assert -3.0 < out.mean() < 3.0
