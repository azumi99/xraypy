import pytest

from app.config import Settings
from app.dedup.shape_filter import ShapeFeatures
from app.dedup.sqlite_store import SqliteStore


@pytest.mark.asyncio
async def test_existing_selected_capture_review_does_not_consume_waiting_awb(tmp_path):
    store = SqliteStore(Settings(_env_file=None), tmp_path / "capture.sqlite")
    await store.initialize()
    try:
        missed = await store.insert_awb("MISSED")
        shape = ShapeFeatures(1, 1, [0.] * 7)
        review = await store.insert_identity(
            batch_id="batch", track_id=3, status="REVIEW", embedding=None,
            model_name="test", model_version="1", shape=shape, crop_ref=None,
            review_reason="SELECTED_CAPTURE_GRAY_ZONE",
        )
        promoted, paired = await store.promote_to_new(review.id, require_awb=True)
        assert promoted.awb_id is None and paired is None
        with pytest.raises(KeyError):
            await store.promote_to_new(review.id, require_awb=True)
        assert [item.id for item in await store.list_awbs("WAITING")] == [missed.id]
    finally:
        await store.close()
