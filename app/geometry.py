"""Crop geometry: BGRA crop with the parcel contour preserved.

Outside the contour -> alpha 0 (transparent). Inside -> original X-ray
content, so embeddings always see the parcel interior (critical for hard
negatives where only the contents differ).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(slots=True)
class Crop:
    bgra: np.ndarray  # HWC uint8, alpha 0 outside the contour
    polygon: np.ndarray  # Nx2 int32 (frame coordinates)


class CropError(ValueError):
    """Degenerate crop (empty mask, too small) — caller marks REVIEW."""


def crop_by_mask(
    frame_bgr: np.ndarray,
    mask: np.ndarray,
    *,
    padding: int = 6,
    min_side: int = 32,
) -> Crop:
    """Build a BGRA crop from a binary mask; alpha=0 outside the contour."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise CropError("empty mask — no contour found")
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < float(min_side) ** 2 / 4:
        raise CropError("contour area below threshold")

    x, y, w, h = cv2.boundingRect(contour)
    x0, y0 = max(0, x - padding), max(0, y - padding)
    x1, y1 = min(frame_bgr.shape[1], x + w + padding), min(frame_bgr.shape[0], y + h + padding)
    if x1 - x0 < min_side or y1 - y0 < min_side:
        raise CropError(f"crop too small: {x1 - x0}x{y1 - y0}")

    patch = frame_bgr[y0:y1, x0:x1]
    alpha = np.zeros(patch.shape[:2], dtype=np.uint8)
    shifted = contour - np.array([x0, y0], dtype=np.int64)
    cv2.fillPoly(alpha, [shifted.astype(np.int32)], 255)
    bgra = np.dstack([patch, alpha])
    polygon = contour.reshape(-1, 2).astype(np.int32)
    return Crop(bgra=bgra, polygon=polygon)


def encode_png(bgra: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgra)
    if not ok:
        raise CropError("PNG encoding failed")
    return buf.tobytes()
