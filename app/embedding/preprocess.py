"""BGRA crop -> normalized model input.

Background (alpha) handling is explicit — never silently dropped:
- ``black``: pixels outside the parcel contour are zeroed (default).
- ``keep``:  alpha is ignored, raw RGB is used everywhere.
- ``mean``:  background is blended with the crop's own mean color.

Resize modes (all use explicit cv2 interpolation, documented):
- ``fill``:   stretch the crop to img_size x img_size (distorts aspect).
- ``contain``: scale to fit, pad the remainder with the background color.
- ``cover``:  scale to fill, center-crop the overflow.
"""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np

BgMode = Literal["black", "keep", "mean"]
ResizeMode = Literal["fill", "contain", "cover"]

# ImageNet statistics — used by both ResNet (torchvision) and DINOv2 (HF).
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def composite_background(bgra: np.ndarray, bg_mode: BgMode) -> np.ndarray:
    """Apply the configured transparent-background treatment -> RGB uint8 HWC."""
    if bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError(f"expected BGRA HWC image with 4 channels, got shape {bgra.shape}")
    bgr = bgra[:, :, :3]
    alpha = bgra[:, :, 3:4].astype(np.float32) / 255.0
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if bg_mode == "keep":
        return rgb
    if bg_mode == "black":
        return (rgb.astype(np.float32) * alpha).astype(np.uint8)
    if bg_mode == "mean":
        visible = rgb.astype(np.float32) * alpha
        denom = float(alpha.sum())
        mean = visible.sum(axis=(0, 1)) / denom if denom > 0 else np.zeros(3, dtype=np.float32)
        bg = np.ones_like(rgb, dtype=np.float32) * mean
        return (visible + bg * (1.0 - alpha)).astype(np.uint8)
    raise ValueError(f"unknown bg_mode: {bg_mode!r}")


def resize_rgb(rgb: np.ndarray, size: int, mode: ResizeMode) -> np.ndarray:
    """Resize an RGB uint8 HWC image to (size, size) with the configured mode."""
    h, w = rgb.shape[:2]
    if mode == "fill":
        return cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)
    if mode == "contain":
        scale = min(size / w, size / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((size, size, 3), dtype=rgb.dtype)  # pad = black
        y0, x0 = (size - nh) // 2, (size - nw) // 2
        canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
        return canvas
    if mode == "cover":
        scale = max(size / w, size / h)
        nw, nh = max(size, round(w * scale)), max(size, round(h * scale))
        resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        y0, x0 = (nh - size) // 2, (nw - size) // 2
        return resized[y0 : y0 + size, x0 : x0 + size]
    raise ValueError(f"unknown resize_mode: {mode!r}")


def preprocess(
    bgra: np.ndarray,
    *,
    img_size: int = 224,
    bg_mode: BgMode = "black",
    resize_mode: ResizeMode = "fill",
    mean: tuple[float, ...] = IMAGENET_MEAN,
    std: tuple[float, ...] = IMAGENET_STD,
) -> np.ndarray:
    """Full preprocessing: BGRA -> float32 CHW normalized tensor-ready array."""
    rgb = composite_background(bgra, bg_mode)
    rgb = resize_rgb(rgb, img_size, resize_mode)
    x = rgb.astype(np.float32) / 255.0
    x = (x - np.asarray(mean, dtype=np.float32)) / np.asarray(std, dtype=np.float32)
    return np.ascontiguousarray(x.transpose(2, 0, 1))  # HWC -> CHW
