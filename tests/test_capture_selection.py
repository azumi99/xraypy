import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from app.capture_selection import CaptureSelection
from app.config import Settings
from app.dedup.shape_filter import ShapeFeatures
from app.dedup.sqlite_store import SqliteStore
from app.dedup.store import AwbRecord, AwbRequiredError
from app.pipeline import Pipeline


@pytest.mark.asyncio
async def test_confirmed_capture_only_pairs_explicit_awb(tmp_path):
    store = SqliteStore(Settings(_env_file=None), tmp_path / "capture.sqlite")
    await store.initialize()
    try:
        missed = await store.insert_awb("MISSED")
        selected = await store.insert_awb("SELECTED")
        args = dict(batch_id="batch", track_id=1, embedding=np.ones(4, dtype=np.float32),
                    model_name="test", model_version="1", shape=ShapeFeatures(1, 1, [0.] * 7),
                    crop_ref="batch/test.png", sim_score=None, require_awb=False, target_awb_id=selected.id)
        record, paired = await store.finalize_new(**args)
        assert record.awb_id == paired.id == selected.id
        with pytest.raises(AwbRequiredError):
            await store.finalize_new(**{**args, "track_id": 2})
        assert await store.get_by_track("batch", 2) is None
        assert [item.id for item in await store.list_awbs("WAITING")] == [missed.id]

        review = await store.insert_identity(
            batch_id="batch", track_id=3, status="REVIEW", embedding=None,
            model_name="test", model_version="1", shape=args["shape"], crop_ref=None,
            review_reason="SELECTED_CAPTURE_GRAY_ZONE",
        )
        promoted, paired = await store.promote_to_new(review.id, require_awb=True)
        assert promoted.awb_id is None and paired is None
        with pytest.raises(KeyError):
            await store.promote_to_new(review.id, require_awb=True)
        assert [item.id for item in await store.list_awbs("WAITING")] == [missed.id]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missed_capture_holds_following_candidate_without_pairing():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    pipeline.capture_selection = selection = CaptureSelection()
    pipeline._commit_track_capture = AsyncMock(return_value=True)
    a = AwbRecord(1, "A", None, "WAITING", None)
    b = AwbRecord(2, "B", None, "WAITING", None)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    async def stage(matched):
        side_ready = asyncio.get_running_loop().create_future()
        side_ready.set_result(matched)
        await pipeline._stage_selected_capture(frame, {
            "batch_id": "batch", "selection_revision": selection.revision,
            "side_ready": side_ready, "side": frame, "captured_at": datetime.now().astimezone(),
        })

    pipeline.select_capture_awb(a)
    await stage(False)
    assert selection.state == "review" and selection.awb.id == a.id
    await stage(True)
    assert selection.state == "review"
    pipeline._commit_track_capture.assert_not_awaited()

    selection.clear()
    assert selection.state == "unassigned" and selection.awb is None
    pipeline.select_capture_awb(b)
    await pipeline.confirm_selected_capture()
    assert pipeline._commit_track_capture.await_args.kwargs == {"target_awb_id": b.id}
    assert selection.state == "idle"
    with pytest.raises(RuntimeError):
        await pipeline.confirm_selected_capture()


@pytest.mark.asyncio
async def test_manual_capture_uses_frozen_frames_and_crops_both_views():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    pipeline.capture_selection = CaptureSelection()
    pipeline._commit_track_capture = AsyncMock(return_value=True)
    frame = np.arange(100 * 100 * 3, dtype=np.uint8).reshape(100, 100, 3)
    original = frame.copy()
    pipeline.select_capture_awb(AwbRecord(1, "A", None, "WAITING", None))
    captured_at = datetime.now().astimezone()
    pipeline._latest_capture_frames = (time.monotonic(), frame, frame, captured_at)
    pipeline.freeze_capture_frames()
    frame[:] = 0
    await pipeline.confirm_selected_capture(((0.1, 0.2, 0.6, 0.8), (0, 0, 0.5, 0.5)))
    args = pipeline._commit_track_capture.await_args
    np.testing.assert_array_equal(args.args[0], original[20:80, 10:60])
    np.testing.assert_array_equal(args.args[1]["side"], original[:50, :50])
    assert args.args[1]["captured_at"] == captured_at
    assert args.kwargs == {"target_awb_id": 1}
