"""Shape filter + idx ordering + AWB FIFO pairing."""

from __future__ import annotations

import numpy as np

from app.config import ShapeFilterConfig
from app.dedup.shape_filter import (
    ShapeFeatures,
    candidates_match,
    compute_shape_features,
    hu_distance,
)
from tests.conftest import make_bgra


def test_compute_shape_features():
    bgra = np.zeros((100, 100, 4), dtype=np.uint8)
    bgra[20:80, 20:80, 3] = 255
    f = compute_shape_features(bgra)
    assert f.area == 3600
    assert np.isclose(f.aspect_ratio, 1.0)
    assert len(f.hu) == 7


def test_candidates_match_boundaries():
    cfg = ShapeFilterConfig(area_tol=0.25, aspect_tol=0.15, hu_max_dist=0.5)
    new = ShapeFeatures(area=1000.0, aspect_ratio=2.0, hu=[0.0] * 7)
    assert candidates_match(new, 1100.0, 2.05, [0.0] * 7, cfg)  # within tolerances
    assert not candidates_match(new, 2000.0, 2.0, [0.0] * 7, cfg)  # area off
    assert not candidates_match(new, 1000.0, 3.0, [0.0] * 7, cfg)  # aspect off
    assert not candidates_match(new, 1000.0, 2.0, [1.0] * 7, cfg)  # hu off
    assert candidates_match(new, None, None, None, cfg)  # missing -> pass-through


def test_hu_distance_length_mismatch():
    assert hu_distance([0.1], []) == float("inf")


async def test_idx_sequential_no_gaps(dedup, store):
    for i in range(5):
        d = await dedup.process_parcel(
            batch_id="B", track_id=i + 1, crop_bgra=make_bgra(seed=100 + i, value=60 + i * 20)
        )
        assert d.status == "NEW", f"parcel {i}: {d.status}"
    idxs = []
    for tid in range(1, 6):
        rec = await store.get_by_track("B", tid)
        idxs.append(rec.idx)
    assert idxs == [0, 1, 2, 3, 4]


async def test_last_idx_resume(dedup, store):
    await dedup.process_parcel(batch_id="B", track_id=1, crop_bgra=make_bgra(seed=1))
    await dedup.process_parcel(batch_id="B", track_id=2, crop_bgra=make_bgra(seed=2, value=160))
    assert await store.last_idx("B") == 1
    assert await store.last_idx("unknown") == -1


async def test_awb_fifo_order(dedup, store):
    for n in ("A1", "A2", "A3"):
        await store.insert_awb(n)
    got = []
    for i in range(3):
        d = await dedup.process_parcel(
            batch_id="B", track_id=i + 1, crop_bgra=make_bgra(seed=200 + i, value=70 + i * 30)
        )
        assert d.status == "NEW", d.status
        got.append(d.awb.awb_number)
    assert got == ["A1", "A2", "A3"]


async def test_more_parcels_than_awbs(dedup, store):
    await store.insert_awb("ONLY")
    d1 = await dedup.process_parcel(batch_id="B", track_id=1, crop_bgra=make_bgra(seed=1))
    d2 = await dedup.process_parcel(batch_id="B", track_id=2, crop_bgra=make_bgra(seed=2, value=180))
    assert d1.awb.awb_number == "ONLY"
    assert d2.status == "NEW"
    assert d2.awb is None  # unpaired, but NOT blocked
