"""Dedup decisions: boundaries, duplicate/review/new, failure degradation."""

from __future__ import annotations

import pytest

from app.dedup.service import DedupService
from app.embedding.worker import QueueFullError
from tests.conftest import make_bgra


async def test_new_parcel_gets_idx_and_awb(dedup, store):
    await store.insert_awb("AWB-001")
    d = await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=1))
    assert d.status == "NEW"
    assert d.record.idx == 0
    assert d.awb is not None and d.awb.awb_number == "AWB-001"


async def test_identical_parcel_is_duplicate_without_idx(dedup, store):
    await store.insert_awb("AWB-001")
    first = await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=42))
    assert first.status == "NEW"
    second = await dedup.process_parcel(batch_id="b1", track_id=2, crop_bgra=make_bgra(seed=42))
    assert second.status == "DUPLICATE"
    assert second.record.idx is None
    assert second.record.duplicate_of == first.record.id
    # different physical parcel within the batch — should not be duplicate
    other = await dedup.process_parcel(batch_id="b1", track_id=3, crop_bgra=make_bgra(value=200, seed=7))
    assert other.status == "NEW"
    assert other.record.idx == 1  # no idx gap: duplicate did not consume a number
    # AWB pairing stays in order: first NEW took AWB-001, second takes the next


class FixedSimStore:
    """Wraps a store; find_similar always returns one match with a fixed sim."""

    def __init__(self, inner, sim: float):
        self._inner = inner
        self._sim = sim

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def find_similar(self, embedding, **kw):
        from app.dedup.store import SimilarMatch

        rec = await self._inner.insert_identity(
            batch_id="seed",
            track_id=-abs(hash(embedding.tobytes())) % 10**6,
            status="NEW",
            embedding=embedding,
            model_name=kw["model_name"],
            model_version=kw["model_version"],
            shape=kw.get("shape") if kw.get("shape") is not None else _SHAPE,
            crop_ref=None,
        )
        return [SimilarMatch(rec, self._sim, 0)]


from app.dedup.shape_filter import ShapeFeatures as _SF  # noqa: E402

_SHAPE = _SF(area=5000.0, aspect_ratio=1.4, hu=[0.1] * 7)


@pytest.mark.parametrize(
    ("sim", "expected", "reason"),
    [
        (0.92, "DUPLICATE", None),  # exactly t_high -> DUPLICATE
        (0.80, "REVIEW", "GRAY_ZONE"),  # exactly t_low -> REVIEW (safe bias)
        (0.7999, "NEW", None),
        (0.9199, "REVIEW", "GRAY_ZONE"),
    ],
)
async def test_similarity_boundaries(dedup, sim, expected, reason):
    svc = DedupService(dedup._settings, dedup._worker, FixedSimStore(dedup._store, sim))
    d = await svc.process_parcel(batch_id="bb", track_id=50, crop_bgra=make_bgra())
    assert d.status == expected
    if reason:
        assert d.record.review_reason == reason


async def test_crop_invalid_is_review(dedup):
    tiny = make_bgra(w=10, h=8)  # below min_crop_side_px
    d = await dedup.process_parcel(batch_id="b1", track_id=5, crop_bgra=tiny)
    assert d.status == "REVIEW"
    assert d.record.review_reason == "CROP_INVALID"


async def test_embedding_queue_full_degrades_to_review(dedup):

    class FullWorker:
        model_name = "stub"
        model_version = "v"
        dim = 64

        async def embed(self, img):
            raise QueueFullError("full")

    svc = DedupService(dedup._settings, FullWorker(), dedup._store)  # type: ignore[arg-type]
    d = await svc.process_parcel(batch_id="b1", track_id=6, crop_bgra=make_bgra())
    assert d.status == "REVIEW"
    assert d.record.review_reason == "QUEUE_FULL"


async def test_embedding_crash_degrades_to_review(dedup):
    class CrashWorker:
        model_name = "stub"
        model_version = "v"
        dim = 64

        async def embed(self, img):
            raise RuntimeError("CUDA OOM")

    svc = DedupService(dedup._settings, CrashWorker(), dedup._store)  # type: ignore[arg-type]
    d = await svc.process_parcel(batch_id="b1", track_id=7, crop_bgra=make_bgra())
    assert d.status == "REVIEW"
    assert d.record.review_reason == "EMBEDDING_FAILED"


async def test_idempotent_reprocessing(dedup):
    first = await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=3))
    again = await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=3))
    assert first.record.id == again.record.id
    assert first.status == again.status


async def test_scope_batch_does_not_cross_batches(dedup, settings):
    settings.dedup.scope = "batch"
    await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=11))
    d = await dedup.process_parcel(batch_id="b2", track_id=1, crop_bgra=make_bgra(seed=11))
    assert d.status == "NEW"


async def test_shape_filter_blocks_false_duplicate(dedup, store, settings):
    await dedup.process_parcel(batch_id="b1", track_id=1, crop_bgra=make_bgra(seed=5))
    # force a shape mismatch on the stored row so the pre-filter rejects it
    row = await store.get_by_track("b1", 1)
    row.area = 10.0
    # NOTE: sqlite store returns a copy; check the filter function directly
    from app.config import ShapeFilterConfig
    from app.dedup.shape_filter import ShapeFeatures, candidates_match

    new = ShapeFeatures(area=90000.0, aspect_ratio=1.4, hu=[0.1] * 7)
    assert not candidates_match(new, row.area, row.aspect_ratio, row.hu_moments, ShapeFilterConfig())
