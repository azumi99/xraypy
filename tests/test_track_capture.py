import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cv2
import numpy as np
import pytest

import app.pipeline as pipeline_module
from app.config import Settings
from app.detector import ParcelTracker
from app.pipeline import Pipeline, _save_capture


def test_track_survives_missed_frame_and_captures_once(tmp_path):
    async def run():
        settings = Settings(crop_dir=tmp_path)
        settings.detector.min_track_hits = 2
        pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
        pipeline.session.batch_id = "test"
        frame = np.zeros((100, 400, 3), dtype=np.uint8)
        detections = iter([
            [SimpleNamespace(parcel_id=1, bbox=(0, 10, 80, 80), conf=0.9)],
            [],
            [SimpleNamespace(parcel_id=1, bbox=(40, 10, 120, 80), conf=0.9)],
            [],
            [SimpleNamespace(parcel_id=1, bbox=(50, 10, 130, 80), conf=0.9)],
        ])
        pipeline._detector = SimpleNamespace(process=lambda _: next(detections))
        captures = []

        async def capture(det, top, side, batch_id):
            captures.append((det.parcel_id, batch_id))

        pipeline._handle_track_parcel = capture
        for _ in range(5):
            await pipeline._process_track_frame({"top": frame})
            await asyncio.sleep(0)
        assert captures == [(1, "test")]

    asyncio.run(run())


def test_held_top_box_does_not_trigger_capture(tmp_path):
    async def run():
        pipeline = Pipeline(Settings(crop_dir=tmp_path), MagicMock(), MagicMock(), MagicMock())
        detections = iter([
            [SimpleNamespace(parcel_id=1, bbox=(30, 10, 80, 40), conf=0.9, observed=True)],
            [SimpleNamespace(parcel_id=1, bbox=(30, 10, 80, 40), conf=0.9, observed=False)],
        ])
        pipeline._detector = SimpleNamespace(process=lambda _: next(detections))
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        await pipeline._process_track_frame({"top": frame})
        await pipeline._process_track_frame({"top": frame})
        assert 1 in pipeline.overlays
        assert pipeline._track_states[1]["hits"] == 1
        assert not pipeline._pending_top

    asyncio.run(run())


async def test_top_capture_waits_for_left_clearance(tmp_path):
    pipeline = Pipeline(Settings(_env_file=None, crop_dir=tmp_path), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "test"
    frame = np.zeros((100, 400, 3), dtype=np.uint8)
    boxes = iter([(0, 10, 60, 70), (25, 10, 105, 70), (40, 10, 140, 70), (60, 10, 160, 70)])
    pipeline._detector = SimpleNamespace(process=lambda _: [SimpleNamespace(parcel_id=1, bbox=next(boxes), conf=0.9)])
    pipeline._capture_with_effect = AsyncMock()
    for _ in range(2):
        await pipeline._process_track_frame({"top": frame})
        assert not pipeline._pending_top
    for _ in range(2):
        await pipeline._process_track_frame({"top": frame})
    await asyncio.sleep(0)
    pipeline._capture_with_effect.assert_awaited_once()
    assert pipeline._capture_with_effect.await_args.args[0].bbox == (40, 13, 140, 73)


def test_blob_assist_detects_parcel_without_detecting_control_panel():
    backend = SimpleNamespace(process=lambda _: [], reset=lambda: None)
    tracker = ParcelTracker(Settings().detector, backend)
    frame = np.full((360, 640, 3), 220, dtype=np.uint8)
    frame[100:240, 40:160] = 70
    frame[310:] = 40
    detections = tracker.process(frame)
    assert len(detections) == 1
    x1, y1, x2, y2 = detections[0].bbox
    assert x1 <= 45 and y1 <= 105 and x2 >= 155 and y2 >= 235
    assert y2 < 310


def test_side_box_uses_its_own_detection(tmp_path):
    async def run():
        pipeline = Pipeline(Settings(crop_dir=tmp_path), MagicMock(), MagicMock(), MagicMock())
        pipeline._detector = SimpleNamespace(process=lambda _: [])
        parcel = SimpleNamespace(parcel_id=3, bbox=(40, 10, 100, 25), conf=0.9)
        pipeline._side_detector = SimpleNamespace(process=lambda _: [parcel])
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        await pipeline._process_track_frame({"top": frame, "side": frame})
        assert pipeline._annotated_side.shape == frame.shape
        assert pipeline._annotated_side[35, 40].any()

    asyncio.run(run())


def test_capture_effect_clears_after_failure(tmp_path):
    async def run():
        pipeline = Pipeline(Settings(crop_dir=tmp_path), MagicMock(), MagicMock(), MagicMock())
        state = {"capturing": True}

        async def fail(*args):
            raise ValueError("capture failed")

        pipeline._handle_track_parcel = fail
        try:
            await pipeline._capture_with_effect(None, None, None, "test", state)
        except ValueError:
            pass
        assert not state["capturing"]

    asyncio.run(run())


def test_independent_side_pairing_and_color_crops(tmp_path, monkeypatch):
    async def run():
        labels = []
        times = iter([datetime(2020, 1, 1, 0, 0, second, tzinfo=UTC) for second in (1, 2, 3, 4)])
        monkeypatch.setattr(pipeline_module, "datetime", SimpleNamespace(now=lambda: next(times)))
        put_text = cv2.putText

        def record_text(image, text, origin, *args):
            labels.append(text)
            return put_text(image, text, origin, *args)

        monkeypatch.setattr(cv2, "putText", record_text)
        dedup = MagicMock()
        dedup.store.get_by_track = AsyncMock(return_value=None)
        dedup.worker.embed = AsyncMock(return_value=None)
        gates = {1: asyncio.Event(), 2: asyncio.Event()}
        saved = {1: asyncio.Event(), 2: asyncio.Event()}

        async def add_side_capture(parcel_id, *_args, **_kwargs):
            saved[parcel_id].set()

        dedup.store.add_side_capture = AsyncMock(side_effect=add_side_capture)

        async def process_parcel(**kwargs):
            await gates[kwargs["track_id"]].wait()
            i = kwargs["track_id"]
            awb = SimpleNamespace(id=i, parcel_id=i, status="PAIRED", awb_number=f"M{i}", hawb_number=f"H{i}", part=i, total=2)
            return SimpleNamespace(record=SimpleNamespace(id=i, batch_id="batch", track_id=i, crop_ref=kwargs["crop_ref"], awb_id=i), awb=awb)

        dedup.process_parcel = process_parcel
        pipeline = Pipeline(Settings(crop_dir=tmp_path, track_roi={"top_start": 0, "top_end": 1, "side_start": 0, "side_end": 1}), dedup, MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        pipeline._apply_decision = AsyncMock()
        top = np.zeros((100, 400, 3), dtype=np.uint8)
        side = np.full((100, 400, 3), (12, 34, 56), dtype=np.uint8)

        def det(i, x):
            return SimpleNamespace(parcel_id=i, bbox=(x, 10, x + 80, 80), conf=0.9)

        top_frames = iter([[det(1, 0)], [det(1, 40)], [det(2, 0)], [det(2, 40)]])
        side_frames = iter([[det(10, 0)], [det(10, 40)], [], [det(20, 0)], [det(20, 40)]])
        pipeline._detector = SimpleNamespace(process=lambda _: next(top_frames))
        pipeline._side_detector = SimpleNamespace(process=lambda _: next(side_frames))
        # First side arrives before top, second side after both top triggers.
        await pipeline._process_track_frame({"side": side})
        await pipeline._process_track_frame({"side": side})
        first_side_time = pipeline._pending_side[0]["captured_at"]
        assert first_side_time.second == 1
        base = pipeline_module.time.monotonic() + 10
        pipeline._pending_side[0]["time"] = base
        for _ in range(2):
            await pipeline._process_track_frame({"top": top})
        first_top_time = pipeline._pending_top[0]["captured_at"]
        assert first_top_time.second == 2
        pipeline._pending_top[0]["time"] = base
        for _ in range(2):
            await pipeline._process_track_frame({"top": top})
        pipeline._pending_top[1]["time"] = base + 10
        await pipeline._process_track_frame({"side": side})
        await pipeline._process_track_frame({"side": side})
        await pipeline._process_track_frame({"side": side})
        pipeline._pending_side[1]["time"] = base + 10
        pipeline._match_track_captures(base + 12)
        gates[2].set()
        await asyncio.wait_for(saved[2].wait(), timeout=3)
        gates[1].set()
        await asyncio.wait_for(saved[1].wait(), timeout=3)
        calls = dedup.store.add_side_capture.await_args_list
        assert {c.args[0]: c.args[2] for c in calls} == {
            1: "batch/seq_1_side.png", 2: "batch/seq_2_side.png"
        }
        image = cv2.imread(str(tmp_path / "batch/seq_1_side.png"), cv2.IMREAD_UNCHANGED)
        assert np.all(image[:91, :105] == (12, 34, 56, 255))
        assert np.all(dedup.worker.embed.await_args_list[0].args[0] == (12, 34, 56, 255))
        assert len(calls) == 2
        rendered = "".join(labels)
        for i in (1, 2):
            for view in ("TOP", "SIDE"):
                assert f"M{i} | H{i} | {i} | 2 | {view}" in rendered

    asyncio.run(run())


def test_strip_trigger_passes_frame_timestamp(tmp_path, monkeypatch):
    async def run():
        when = datetime(2020, 2, 3, 4, 5, 6, tzinfo=UTC)
        monkeypatch.setattr(pipeline_module, "datetime", SimpleNamespace(now=lambda: when))
        pipeline = Pipeline(Settings(crop_dir=tmp_path), MagicMock(), MagicMock(), MagicMock())
        parcel = SimpleNamespace(seq=1)
        pipeline._tracker.update = MagicMock(return_value=[parcel])
        pipeline._sync_overlays = MagicMock()
        pipeline._annotate = MagicMock()
        pipeline._handle_new_parcel = AsyncMock()
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        await pipeline._process_strip_frame({"top": frame, "side": frame})
        await asyncio.sleep(0)
        assert pipeline._handle_new_parcel.await_args.args[3] == when

    asyncio.run(run())


def test_capture_long_fields_and_tiny_image(tmp_path, monkeypatch):
    image = np.array([[[4, 5, 6, 7]]], dtype=np.uint8)
    awb = SimpleNamespace(awb_number="M" * 130, hawb_number="h" * 140, part=123)
    captured = datetime(2020, 2, 3, 4, 5, 6, tzinfo=UTC)
    calls = []
    put_text = cv2.putText

    def record(image, text, origin, *args):
        calls.append((text, origin))
        return put_text(image, text, origin, *args)

    monkeypatch.setattr(cv2, "putText", record)
    path = tmp_path / "tiny.png"
    _save_capture(path, image, "side", awb, captured_at=captured)
    saved = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert sum(np.all(saved == image[0, 0], axis=2).flat) == 1
    rendered = "".join(text for text, _ in calls)
    assert rendered == "M" * 130 + " | " + "H" * 140 + " | 123 | - | SIDE"


def test_timestamp_pairing_rejects_ambiguous_and_stale(tmp_path):
    async def run():
        settings = Settings(crop_dir=tmp_path)
        settings.capture.pairing_max_skew_s = 0.5
        settings.capture.pairing_side_delay_s = 2
        pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        paired = []
        pipeline._store_paired_side = lambda slot: paired.append((slot["seq"], slot["side"]))

        def top(seq, t, batch="batch"):
            pipeline._queue_track_capture(pipeline._pending_top, {"batch_id": batch, "seq": seq, "time": t, "side": None})

        def side(name, raw_time, batch="batch"):
            pipeline._queue_track_capture(pipeline._pending_side, {"batch_id": batch, "time": raw_time - 2, "side": name})

        # Side-first and top-first; arrivals need not be timestamp ordered.
        side("b", 12.3)
        top(1, 10)
        side("a", 12)
        top(2, 10.3)
        pipeline._match_track_captures(13)
        assert paired == []  # both candidates are ambiguous, despite differing distances
        assert not pipeline._pending_top and not pipeline._pending_side

        # A missed side must not shift the next match to the oldest top.
        top(3, 20)
        top(4, 22)
        side("d", 24.1)
        pipeline._match_track_captures(25)
        assert paired == [(4, "d")]
        assert not pipeline._pending_top and not pipeline._pending_side

        # Independent close events remain ordered when windows do not overlap.
        side("f", 33)
        top(5, 30)
        top(6, 31)
        side("e", 32)
        pipeline._match_track_captures(34)
        assert paired[-2:] == [(5, "e"), (6, "f")]

        top(7, 40, "old")
        side("new", 42)
        pipeline._match_track_captures(43)
        assert len(paired) == 3
        assert not pipeline._pending_top and not pipeline._pending_side

    asyncio.run(run())


@pytest.mark.parametrize("view", ["top", "side"])
def test_saved_watermark_overlay_preserves_original_and_existing_file(tmp_path, monkeypatch, view):
    original = np.random.default_rng(7).integers(0, 256, (60, 320, 4), dtype=np.uint8)
    copy = original.copy()
    calls = []
    put_text = cv2.putText

    def record_text(image, text, origin, *args):
        calls.append((text, origin))
        return put_text(image, text, origin, *args)

    monkeypatch.setattr(cv2, "putText", record_text)
    path = tmp_path / "capture.png"
    _save_capture(path, original, view)
    saved = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert np.array_equal(original, copy)
    assert np.array_equal(saved[:60, :320], copy)
    assert "".join(call[0] for call in calls) == f"- | - | - | - | {view.upper()}"
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        _save_capture(path, np.zeros_like(original), view)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["track", "strip"])
def test_capture_modes_save_both_views_without_watermarking_dedup(tmp_path, monkeypatch, mode):
    async def run():
        dedup = MagicMock()
        dedup.store.get_by_track = AsyncMock(return_value=None)
        awb = SimpleNamespace(id=7, parcel_id=1, status="PAIRED", awb_number="123", hawb_number="h42", part=2, total=5)
        record = SimpleNamespace(id=1, batch_id="batch", track_id=1, crop_ref="batch/seq_1_top.png", awb_id=7)
        dedup.process_parcel = AsyncMock(return_value=SimpleNamespace(record=record, awb=awb))
        dedup.worker.embed = AsyncMock(return_value=None)
        dedup.store.add_side_capture = AsyncMock()
        pipeline = Pipeline(Settings(crop_dir=tmp_path), dedup, MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        pipeline._apply_decision = AsyncMock()
        labels = []
        put_text = cv2.putText

        def record_text(image, text, origin, *args):
            labels.append(text)
            return put_text(image, text, origin, *args)

        monkeypatch.setattr(cv2, "putText", record_text)
        frame = np.full((100, 400, 3), (12, 34, 56), dtype=np.uint8)
        if mode == "track":
            slot = {"batch_id": "batch", "seq": 1, "side": frame.copy(), "decision": None}
            await pipeline._handle_track_parcel(SimpleNamespace(parcel_id=1, bbox=(40, 10, 120, 80)), frame, slot, "batch")
            await asyncio.sleep(0.1)
        else:
            parcel = SimpleNamespace(seq=1, patch_bgr=frame.copy(), x1=0, x2=100)
            await pipeline._handle_new_parcel(parcel, frame.shape, frame)
        rendered = "".join(labels)
        assert "123 | H42 | 2 | 5 | TOP" in rendered
        assert "123 | H42 | 2 | 5 | SIDE" in rendered
        for view, source in (("top", dedup.process_parcel.await_args.kwargs["crop_bgra"]),
                             ("side", dedup.worker.embed.await_args.args[0])):
            saved = cv2.imread(str(tmp_path / f"batch/seq_1_{view}.png"), cv2.IMREAD_UNCHANGED)
            assert np.array_equal(saved[:source.shape[0], :source.shape[1]], source)

    asyncio.run(run())


def test_stop_discards_unmatched_captures(tmp_path):
    async def run():
        bus = SimpleNamespace(publish=AsyncMock())
        pipeline = Pipeline(Settings(crop_dir=tmp_path), MagicMock(), bus, MagicMock())
        pipeline.session.batch_id = "old"
        pipeline.session.running = True
        pipeline._pending_top.append({"batch_id": "old"})
        pipeline._pending_side.append(np.zeros((10, 10, 3), dtype=np.uint8))
        pipeline._side_track_states[1] = {"captured": True}
        await pipeline.stop_session()
        assert not pipeline._pending_top
        assert not pipeline._pending_side
        assert not pipeline._side_track_states

    asyncio.run(run())


def test_capture_awb_requires_exact_identity_and_handles_lookup_failure(tmp_path):
    async def run():
        dedup = MagicMock()
        pipeline = Pipeline(Settings(crop_dir=tmp_path), dedup, MagicMock(), MagicMock())
        record = SimpleNamespace(id=3, awb_id=9)
        other = SimpleNamespace(id=9, parcel_id=4, status="PAIRED")
        match = SimpleNamespace(id=9, parcel_id=3, status="PAIRED")
        decision = SimpleNamespace(record=record, awb=other)
        assert await pipeline._capture_awb(decision) is None
        decision.awb = None
        dedup.store.list_awbs = AsyncMock(return_value=[other, match])
        assert await pipeline._capture_awb(decision) is None
        dedup.store.list_awbs = AsyncMock(return_value=[match])
        assert await pipeline._capture_awb(decision) is match
        dedup.store.list_awbs = AsyncMock(side_effect=RuntimeError("offline"))
        assert await pipeline._capture_awb(decision) is None

    asyncio.run(run())


def test_failed_top_does_not_assign_its_side_to_next_parcel(tmp_path):
    async def run():
        dedup = MagicMock()
        dedup.store.get_by_track = AsyncMock(return_value=None)
        dedup.process_parcel = AsyncMock(side_effect=RuntimeError("failed"))
        pipeline = Pipeline(Settings(crop_dir=tmp_path), dedup, MagicMock(), MagicMock())
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        slot = {"batch_id": "batch", "seq": 1, "side": frame.copy(), "decision": None}
        await pipeline._handle_track_parcel(SimpleNamespace(parcel_id=1, bbox=(40, 10, 120, 80)), frame, slot, "batch")
        assert slot["decision"] is None
        assert (tmp_path / "batch/seq_1_top.png").exists()
        assert not pipeline._pending_side

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["track", "strip"])
@pytest.mark.parametrize("failure", ["original", "watermark"])
def test_capture_write_failures_keep_database_references_valid(tmp_path, monkeypatch, mode, failure):
    async def run():
        dedup = MagicMock()
        dedup.store.get_by_track = AsyncMock(return_value=None)
        dedup.store.add_side_capture = AsyncMock()
        dedup.worker.embed = AsyncMock(return_value=None)
        record = SimpleNamespace(id=1, batch_id="batch", track_id=1, crop_ref="batch/seq_1_top.png", awb_id=None)
        path = tmp_path / record.crop_ref

        async def decide(**kwargs):
            saved = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            assert np.array_equal(saved, kwargs["crop_bgra"])
            return SimpleNamespace(record=record, awb=None)

        dedup.process_parcel = AsyncMock(side_effect=decide)
        pipeline = Pipeline(Settings(crop_dir=tmp_path), dedup, MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        pipeline._apply_decision = AsyncMock()

        def fail(*args, **kwargs):
            raise OSError("injected failure")

        monkeypatch.setattr(pipeline_module, "_save_original" if failure == "original" else "_save_capture", fail)
        frame = np.full((100, 400, 3), (12, 34, 56), dtype=np.uint8)
        if mode == "track":
            slot = {"batch_id": "batch", "seq": 1, "side": frame.copy(), "decision": None}
            await pipeline._handle_track_parcel(SimpleNamespace(parcel_id=1, bbox=(40, 10, 120, 80)), frame, slot, "batch")
            await asyncio.sleep(0.1)
        else:
            await pipeline._handle_new_parcel(SimpleNamespace(seq=1, patch_bgr=frame, x1=0, x2=100), frame.shape, frame)
        if failure == "original":
            dedup.process_parcel.assert_not_awaited()
            assert not path.exists()
            assert pipeline.session.parcel_count == 0
        else:
            dedup.process_parcel.assert_awaited_once()
            assert np.array_equal(cv2.imread(str(path), cv2.IMREAD_UNCHANGED), dedup.process_parcel.await_args.kwargs["crop_bgra"])
            assert pipeline.session.parcel_count == 1
            pipeline._apply_decision.assert_awaited_once()
            side_ref = dedup.store.add_side_capture.await_args.args[2]
            assert np.array_equal(cv2.imread(str(tmp_path / side_ref), cv2.IMREAD_UNCHANGED), dedup.worker.embed.await_args.args[0])

    asyncio.run(run())


@pytest.mark.parametrize("existing_identity", [False, True])
def test_existing_capture_or_identity_is_never_overwritten(tmp_path, existing_identity):
    async def run():
        dedup = MagicMock()
        dedup.store.get_by_track = AsyncMock(return_value=object() if existing_identity else None)
        dedup.process_parcel = AsyncMock()
        pipeline = Pipeline(Settings(crop_dir=tmp_path), dedup, MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        path = tmp_path / "batch/seq_1_top.png"
        path.parent.mkdir()
        path.write_bytes(b"existing")
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        await pipeline._handle_track_parcel(SimpleNamespace(parcel_id=1, bbox=(40, 10, 120, 80)), frame, None, "batch")
        dedup.process_parcel.assert_not_awaited()
        assert path.read_bytes() == b"existing"

    asyncio.run(run())


def test_atomic_capture_write_failures_preserve_files(tmp_path, monkeypatch):
    image = np.full((50, 120, 4), 123, dtype=np.uint8)
    path = tmp_path / "capture.png"
    real_link = pipeline_module.os.link

    def fail(*args, **kwargs):
        raise OSError("injected filesystem failure")

    monkeypatch.setattr(pipeline_module.os, "link", fail)
    with pytest.raises(OSError):
        pipeline_module._save_original(path, image)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(pipeline_module.os, "link", real_link)
    owned = pipeline_module._save_original(path, image)
    original = path.read_bytes()
    monkeypatch.setattr(pipeline_module.os, "replace", fail)
    with pytest.raises(OSError):
        _save_capture(path, image, "top", owned=owned)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


async def test_flat_side_capture_waits_until_parcel_clears_left_edge():
    settings = Settings(_env_file=None)
    pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    backend = SimpleNamespace(process=lambda _: [])
    pipeline._side_detector = ParcelTracker(settings.detector, backend, side=True)

    def side_frame(left, right):
        frame = np.full((480, 720, 3), 220, np.uint8)
        frame[380:422, left:right] = (150, 230, 240)
        frame[422:] = 40
        return frame

    for frame in (side_frame(0, 565), side_frame(0, 590), side_frame(0, 600)):
        await pipeline._process_side_track_frame(frame)
        assert not pipeline._pending_side

    for frame in (side_frame(12, 612), side_frame(24, 624)):
        await pipeline._process_side_track_frame(frame)
        assert len(pipeline._pending_side) == 1

    crop = pipeline._pending_side[0]["side"]
    assert tuple(crop[-1, crop.shape[1] // 2]) == (150, 230, 240)


@pytest.mark.parametrize("positions", [[40, 40, 40], [55, 50, 45]])
async def test_side_capture_requires_rightward_motion(positions):
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    boxes = iter(positions)

    def detect(_):
        x = next(boxes)
        return [SimpleNamespace(parcel_id=1, bbox=(x, 50, x + 120, 100), conf=0.9)]

    pipeline._side_detector = SimpleNamespace(process=detect)
    for _ in positions:
        await pipeline._process_side_track_frame(np.full((480, 720, 3), 220, np.uint8))
    assert not pipeline._pending_side


@pytest.mark.parametrize("side_delay", [0.0, 0.75])
async def test_top_capture_saves_visible_side_when_side_trigger_misses(tmp_path, side_delay):
    settings = Settings(_env_file=None, crop_dir=tmp_path)
    dedup = MagicMock(photo_sync=None)
    dedup.store.get_by_track = AsyncMock(return_value=None)
    dedup.store.add_side_capture = AsyncMock()
    dedup.worker.embed = AsyncMock(return_value=None)
    record = SimpleNamespace(id=7, batch_id="batch", track_id=1, crop_ref="batch/seq_1_top.png", awb_id=None)
    dedup.process_parcel = AsyncMock(return_value=SimpleNamespace(record=record, awb=None))
    pipeline = Pipeline(settings, dedup, MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    pipeline._apply_decision = AsyncMock()
    saved = asyncio.Event()
    save_side_capture = pipeline._save_side_capture

    async def save_side(slot, image):
        await save_side_capture(slot, image)
        saved.set()

    pipeline._save_side_capture = save_side
    top_boxes = iter([(0, 10, 80, 100), (45, 10, 125, 100)])
    pipeline._detector = SimpleNamespace(process=lambda _: [
        SimpleNamespace(parcel_id=1, bbox=next(top_boxes), conf=0.9),
    ])
    pipeline._side_detector = SimpleNamespace(process=lambda _: [
        SimpleNamespace(parcel_id=2, bbox=(180, 30, 350, 95), conf=0.9),
    ])
    top = np.zeros((200, 400, 3), dtype=np.uint8)
    side = np.full_like(top, (12, 34, 56))
    for _ in range(2):
        await pipeline._process_track_frame({"top": top})
    top_seconds = pipeline._pending_top[0]["time"]
    pipeline._match_track_captures(top_seconds + side_delay)
    assert len(pipeline._pending_top) == 1
    await pipeline._process_side_track_frame(side, top_seconds + side_delay, "batch")
    assert not pipeline._pending_side  # Side track first appeared past the entry zone.
    pipeline._match_track_captures(top_seconds + side_delay + 2 * settings.capture.pairing_max_skew_s + 0.1)
    await asyncio.wait_for(saved.wait(), timeout=3)

    dedup.store.add_side_capture.assert_awaited_once()
    path = tmp_path / "batch/seq_1_side.png"
    assert tuple(cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[0, 0]) == (12, 34, 56, 255)
    assert (tmp_path / "batch/seq_1_top.png").exists()
    assert not pipeline._side_capture_samples


async def test_missing_side_leaves_awb_waiting(tmp_path):
    settings = Settings(_env_file=None, crop_dir=tmp_path)
    dedup = MagicMock()
    dedup.store.get_by_track = AsyncMock(return_value=None)
    dedup.process_parcel = AsyncMock()
    pipeline = Pipeline(settings, dedup, MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    done = asyncio.Event()
    capture = pipeline._capture_with_effect

    async def capture_and_signal(*args):
        await capture(*args)
        done.set()

    pipeline._capture_with_effect = capture_and_signal
    boxes = iter([(0, 10, 80, 100), (45, 10, 125, 100)])
    pipeline._detector = SimpleNamespace(process=lambda _: [
        SimpleNamespace(parcel_id=1, bbox=next(boxes), conf=0.9),
    ])
    top = np.zeros((200, 400, 3), dtype=np.uint8)
    for _ in range(2):
        await pipeline._process_track_frame({"top": top})
    top_time = pipeline._pending_top[0]["time"]
    pipeline._match_track_captures(top_time + 2 * settings.capture.pairing_max_skew_s + 0.1)
    await asyncio.wait_for(done.wait(), timeout=3)

    assert (tmp_path / "batch/seq_1_top.png").exists()
    dedup.process_parcel.assert_not_awaited()


def test_pairing_resolves_competing_top_detections_by_belt_position():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    paired = []
    pipeline._store_paired_side = lambda slot: paired.append(slot["seq"])
    pipeline._pending_top.extend([
        {"batch_id": "batch", "time": 10.0, "seq": 3, "belt_span": (0.15, 0.65)},
        {"batch_id": "batch", "time": 10.2, "seq": 4, "belt_span": (0.45, 0.66)},
    ])
    pipeline._pending_side.append({
        "batch_id": "batch", "time": 10.1, "track_id": 5,
        "belt_span": (0.16, 0.64), "side": np.zeros((20, 40, 3), np.uint8),
    })
    pipeline._match_track_captures(12.0)

    assert paired == [3]


async def test_side_capture_uses_complete_silhouette_and_best_clearance():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    box = iter([(65, 135, 195, 205), (90, 135, 240, 205), (135, 135, 280, 205), (200, 135, 350, 205)])
    pipeline._side_detector = SimpleNamespace(process=lambda _: [
        SimpleNamespace(parcel_id=1, bbox=next(box), conf=0.9),
    ])

    def frame(left, right):
        result = np.full((480, 720, 3), 220, np.uint8)
        result[230:345, left:right] = 90
        return result

    paired = []
    pipeline._store_paired_side = lambda slot: paired.append(slot["side"])
    pipeline._pending_top.append({"batch_id": "batch", "time": 10.20, "seq": 3})
    await pipeline._process_side_track_frame(frame(0, 205), 10.0, "batch")
    await pipeline._process_side_track_frame(frame(0, 260), 10.1, "batch")
    assert not pipeline._pending_side and not pipeline._side_capture_samples

    await pipeline._process_side_track_frame(frame(12, 318), 10.20, "batch")
    assert len(pipeline._pending_side) == 1
    await pipeline._process_side_track_frame(frame(100, 406), 10.30, "batch")
    assert len(pipeline._pending_side) == 1
    pipeline._match_track_captures(11.0)
    crop, = paired
    ys, xs = np.where(crop[:, :, 0] == 90)
    assert xs.max() - xs.min() + 1 == 306
    assert ys.max() - ys.min() + 1 == 115
    assert min(xs.min(), crop.shape[1] - 1 - xs.max()) >= 40
    assert min(ys.min(), crop.shape[0] - 1 - ys.max()) >= 15


def test_pairing_recovers_side_first_after_regular_window_with_belt_overlap():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    paired = []
    pipeline._store_paired_side = lambda slot: paired.append(slot["seq"])
    image = np.zeros((24, 60, 3), np.uint8)
    pipeline._pending_side.append({
        "batch_id": "batch", "time": 10.0, "track_id": 5, "side": image, "belt_span": (0.22, 0.62),
    })
    pipeline._match_track_captures(10.6)
    assert len(pipeline._pending_side) == 1
    pipeline._pending_top.append({"batch_id": "batch", "time": 10.8, "seq": 3, "belt_span": (0.20, 0.65)})
    pipeline._match_track_captures(11.9)
    assert paired == [3]
