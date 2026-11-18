"""StripTracker: baseline, new-object detection, guards, real screenshot."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.strip_tracker import (
    StripTracker,
    contour_crop_bgra,
    segment_columns,
)

SAMPLE = Path.home() / "Downloads" / "xray_8_top_1792824304409.jpg"


def make_strip(n_objects=3, w=400, h=200, seed=0):
    """Synthetic strip: dark parcels on light 222 background."""
    img = np.full((h, w, 3), 222, dtype=np.uint8)
    rng = np.random.default_rng(seed)
    boxes = []
    x = 10
    for _ in range(n_objects):
        bw, bh = int(rng.integers(50, 90)), int(rng.integers(80, 140))
        img[(h - bh) // 2 : (h + bh) // 2, x : x + bw] = rng.integers(40, 160, (bh, bw, 3), dtype=np.uint8)
        boxes.append((x, x + bw))
        x += bw + 20
    return img, boxes


def test_segment_columns_finds_objects():
    img, boxes = make_strip(3)
    segs = segment_columns(img, white_thr=200, min_gap=6, min_w=25)
    assert len(segs) == 3
    for (s, e), (bs, be) in zip(segs, boxes, strict=False):
        assert abs(s - bs) <= 2 and abs(e - be) <= 2


def test_first_frame_is_baseline():
    img, boxes = make_strip(3)
    t = StripTracker()
    assert t.update(img) == []
    assert len(t.known) == 3
    assert all(k.baseline for k in t.known)


def test_new_object_from_left():
    img, _ = make_strip(2, w=400)
    t = StripTracker()
    t.update(img)
    # strip moves right by a full object+gap (100px) and a new object fully
    # enters on the left at x=10..80
    shifted = np.full_like(img, 222)
    shifted[:, 100:] = img[:, :-100]
    rng = np.random.default_rng(9)
    new_w = 70
    shifted[40:160, 10 : 10 + new_w] = rng.integers(40, 160, (120, new_w, 3), dtype=np.uint8)
    new = t.update(shifted)
    assert len(new) == 1
    assert new[0].seq == 1  # baseline objects have seq 0; new gets 1
    # old objects must not re-capture
    again = t.update(shifted)
    assert again == []


def test_entering_object_not_captured():
    img, _ = make_strip(1, w=300)
    t = StripTracker()
    t.update(img)
    shifted = np.full_like(img, 222)
    shifted[:, 30:] = img[:, :-30]
    # new object partially cut at left edge (starts at x=2 < left_margin)
    rng = np.random.default_rng(3)
    shifted[40:160, 2:80] = rng.integers(40, 160, (120, 78, 3), dtype=np.uint8)
    new = t.update(shifted)
    assert new == []


def test_contour_crop_on_real_screenshot():
    if not SAMPLE.exists():
        pytest.skip("sample screenshot not available")
    img = cv2.imread(str(SAMPLE))
    roi = img[0:308, 2:638]
    segs = segment_columns(roi, white_thr=200, min_gap=6, min_w=25)
    assert len(segs) == 5, f"expected 5 parcels, got {segs}"
    # contour crop of the middle parcel must be usable
    a, b = segs[2]
    patch = roi[:, a : b + 1]
    bgra, polygon = contour_crop_bgra(patch, min_side=32, thr=200)
    assert bgra.shape[2] == 4
    assert bgra[:, :, 3].max() == 255
    assert len(polygon) >= 4
    # crop must contain real content (not just transparent border)
    assert (bgra[:, :, 3] > 0).mean() > 0.3
