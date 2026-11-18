"""Fast shape pre-filter before vector search: area, aspect ratio, Hu moments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import cv2
import numpy as np

from ..config import ShapeFilterConfig


@dataclass(slots=True)
class ShapeFeatures:
    area: float
    aspect_ratio: float  # width / height of the contour bbox
    hu: list[float]  # log-transformed Hu moments (7 values)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def compute_shape_features(bgra: np.ndarray) -> ShapeFeatures:
    """Compute shape metrics from a BGRA crop (alpha encodes the contour)."""
    alpha = bgra[:, :, 3]
    area = float(cv2.countNonZero(alpha))
    if area == 0:
        return ShapeFeatures(area=0.0, aspect_ratio=0.0, hu=[0.0] * 7)
    contours, _ = cv2.findContours(alpha, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return ShapeFeatures(area=area, aspect_ratio=0.0, hu=[0.0] * 7)
    contour = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(contour)
    aspect = float(w) / float(h) if h > 0 else 0.0
    hu = _hu_log(cv2.HuMoments(cv2.moments(contour)).flatten())
    return ShapeFeatures(area=area, aspect_ratio=aspect, hu=[float(v) for v in hu])


def _hu_log(moments: np.ndarray) -> np.ndarray:
    """Sign-preserving log transform (standard practice: -sign(h)*log|h|)."""
    out = np.zeros_like(moments, dtype=np.float64)
    nonzero = moments != 0
    out[nonzero] = -np.sign(moments[nonzero]) * np.log10(np.abs(moments[nonzero]))
    return out


def hu_distance(hu_a: list[float], hu_b: list[float]) -> float:
    """Mean absolute distance between log Hu vectors (inf on length mismatch)."""
    if not hu_a or not hu_b or len(hu_a) != len(hu_b):
        return float("inf")
    a, b = np.asarray(hu_a), np.asarray(hu_b)
    return float(np.mean(np.abs(a - b)))


def candidates_match(
    new: ShapeFeatures,
    other_area: float | None,
    other_aspect: float | None,
    other_hu: list[float] | None,
    cfg: ShapeFilterConfig,
) -> bool:
    """True when the stored parcel is shape-compatible with the new crop."""
    if other_area is None or other_aspect is None:
        return True  # missing metrics -> don't pre-filter it out
    if new.area > 0 and not np.isclose(other_area, 0.0):
        if abs(other_area - new.area) / max(new.area, 1.0) > cfg.area_tol:
            return False
    if new.aspect_ratio > 0:
        if abs(other_aspect - new.aspect_ratio) / max(new.aspect_ratio, 1e-6) > cfg.aspect_tol:
            return False
    if other_hu is not None and hu_distance(new.hu, other_hu) > cfg.hu_max_dist:
        return False
    return True
