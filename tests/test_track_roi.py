import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from app.config import Settings, TrackRoiConfig
from app.detector import ParcelTracker
from app.pipeline import Pipeline


@pytest.mark.parametrize("view,start,end", [("top", 3, 86), ("side", 55, 90)])
def test_track_roi_detection_display_and_capture(view, start, end):
    async def run():
        pipeline = Pipeline(Settings(), MagicMock(), MagicMock(), MagicMock())
        pipeline.session.batch_id = "batch"
        frame = np.broadcast_to(np.arange(100, dtype=np.uint8)[:, None, None], (100, 200, 3)).copy()
        inputs = []
        boxes = iter([(0, 2, 60, 20), (20, 2, 80, 20)])

        def detect(image):
            inputs.append(image.copy())
            return [SimpleNamespace(parcel_id=1, bbox=next(boxes), conf=0.9)]

        setattr(pipeline, "_detector" if view == "top" else "_side_detector", SimpleNamespace(process=detect))
        captured = []

        async def capture(det, image, slot, batch, state):
            captured.append((det.bbox, image))

        pipeline._capture_with_effect = capture
        for _ in range(2):
            await pipeline._process_track_frame({view: frame})
            await asyncio.sleep(0)
        expected = frame[start:end]
        assert all(np.array_equal(image, expected) for image in inputs)
        annotated = pipeline._annotated if view == "top" else pipeline._annotated_side
        assert annotated.shape == (expected.shape if view == "top" else frame.shape)
        annotated_roi = annotated if view == "top" else annotated[start:end]
        assert not np.array_equal(annotated_roi[0:25, 20:81], expected[0:25, 20:81])
        if view == "top":
            bbox, capture_frame = captured[0]
            assert bbox == (20, start + 2, 80, start + 20)
            np.testing.assert_array_equal(capture_frame, frame)
        else:
            assert np.array_equal(annotated[:start], frame[:start])
            assert np.array_equal(annotated[end:], frame[end:])
            patch = pipeline._pending_side[0]["side"]
            assert np.array_equal(patch, expected[0:24, 11:90])

    asyncio.run(run())


@pytest.mark.parametrize("height", [1, 2, 3, 7])
@pytest.mark.parametrize("view", ["top", "side"])
def test_small_frames_have_nonempty_full_width_roi(height, view):
    pipeline = Pipeline(Settings(), MagicMock(), MagicMock(), MagicMock())
    frame = np.zeros((height, 5, 3), np.uint8)
    roi = pipeline._track_roi(frame, view)
    assert 1 <= roi.shape[0] <= height
    assert roi.shape[1] == 5


def test_track_roi_config_environment_and_validation(monkeypatch):
    monkeypatch.setenv("CV_TRACK_ROI__TOP_START", "0.2")
    monkeypatch.setenv("CV_TRACK_ROI__SIDE_END", "0.8")
    cfg = Settings().track_roi
    assert (cfg.top_start, cfg.side_end) == (0.2, 0.8)
    pipeline = Pipeline(Settings(), MagicMock(), MagicMock(), MagicMock())
    frame = np.arange(100, dtype=np.uint8).reshape(100, 1, 1)
    assert np.array_equal(pipeline._track_roi(frame, "top")[:, 0, 0], np.arange(20, 86))
    assert np.array_equal(pipeline._track_roi(frame, "side")[:, 0, 0], np.arange(55, 80))
    for kwargs in ({"top_start": -0.1}, {"side_end": 1.1}, {"top_start": float("nan")},
                   {"top_start": 0.86}, {"side_start": 0.95}):
        with pytest.raises(ValidationError):
            TrackRoiConfig(**kwargs)


@pytest.mark.parametrize("view", ["top", "side"])
def test_blob_near_bottom_of_track_roi_is_not_cut_off(view):
    pipeline = Pipeline(Settings(), MagicMock(), MagicMock(), MagicMock())
    frame = np.full((1000, 640, 3), 220, np.uint8)
    roi = pipeline._track_roi(frame, view)
    h = roi.shape[0]
    roi[int(h * 0.82):int(h * 0.96), 100:250] = 70
    backend = SimpleNamespace(process=lambda _: [], reset=lambda: None)
    detections = ParcelTracker(Settings().detector, backend).process(roi)
    assert len(detections) == 1
    assert detections[0].bbox[3] >= int(h * 0.95) - 2


@pytest.mark.parametrize("view,row,level", [("top", 40, 220), ("side", 80, 195)])
def test_reference_preview_highlights_weak_bridge_without_changing_capture(view, row, level, tmp_path, monkeypatch):
    reference = np.full((100, 400, 3), level, np.uint8)
    path = tmp_path / f"{view}.png"
    assert cv2.imwrite(str(path), reference)
    monkeypatch.setenv(f"CV_DETECTOR__REFERENCE_{view.upper()}", str(path))
    pipeline = Pipeline(Settings(), MagicMock(), MagicMock(), MagicMock())
    frame = reference.copy()
    frame[row:row + 8, 80:160] -= 9
    frame[row:row + 8, 80:100] -= 20
    frame[row:row + 8, 140:160] -= 20
    original = frame.copy()

    def detect(image):
        np.testing.assert_array_equal(image, pipeline._track_roi(original, view))
        return []

    pipeline._detector = pipeline._side_detector = SimpleNamespace(process=detect)
    asyncio.run(pipeline._process_track_frame({view: frame}))
    preview = pipeline._annotated if view == "top" else pipeline._annotated_side
    y = row - 3 if view == "top" else row
    assert preview[y, 120, 0] < preview[y, 120, 1]  # weak yellow bridge
    assert preview[y, 90, 0] > preview[y, 90, 1]  # strong magenta seed
    np.testing.assert_array_equal(preview[y, 170], original[row, 170])
    np.testing.assert_array_equal(frame, original)
    assert not pipeline._pending_top and not pipeline._pending_side


@pytest.mark.parametrize("mode", ["enabled", "preview-only", "unconfigured", "missing", "wrong-size"])
async def test_side_reference_merge_before_ids_keeps_raw_capture(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("CV_TRACK_ROI__SIDE_START", "0.45")
    monkeypatch.setenv("CV_TRACK_ROI__SIDE_END", "0.825")
    if mode != "preview-only":
        monkeypatch.setenv("CV_DETECTOR__SIDE_REFERENCE_MERGE", "true")
    else:
        monkeypatch.delenv("CV_DETECTOR__SIDE_REFERENCE_MERGE", raising=False)
    reference = np.full((800, 428, 3), 160, np.uint8)
    reference[360:660] = 220
    if mode != "unconfigured":
        path = tmp_path / "empty-side.png"
        monkeypatch.setenv("CV_DETECTOR__REFERENCE_SIDE", str(path))
        if mode != "missing":
            # This mismatch still yields the same cropped height and must be rejected.
            image = np.concatenate((reference, reference[-1:])) if mode == "wrong-size" else reference
            assert cv2.imwrite(str(path), image)
    else:
        monkeypatch.delenv("CV_DETECTOR__REFERENCE_SIDE", raising=False)
    settings = Settings(_env_file=None)
    pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
    pipeline.session.batch_id = "batch"
    frame = reference.copy()
    roi = frame[360:660]
    roi[130:190, 199:232, 0] -= 9
    boxes = [(65, 110, 198, 245), (232, 72, 334, 267), (65, 239, 240, 268)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    pipeline._side_detector = ParcelTracker(settings.detector, backend, side=True)
    original = frame.copy()
    await pipeline._process_track_frame({"side": frame})
    first_ids = set(pipeline._side_track_states)
    await pipeline._process_track_frame({"side": frame})
    assert set(pipeline._side_track_states) == first_ids
    assert len(first_ids) == (1 if mode == "enabled" else 2)
    assert all(state["hits"] == 2 for state in pipeline._side_track_states.values())
    assert not pipeline._pending_top
    assert len(pipeline._pending_side) == 1
    candidate = pipeline._pending_side[0]
    assert candidate["batch_id"] == "batch"
    expected = roi[43:298, 25:375] if mode == "enabled" else roi[86:293, 45:219]
    np.testing.assert_array_equal(candidate["side"], expected)
    np.testing.assert_array_equal(frame, original)
    if mode in {"enabled", "preview-only"}:
        assert pipeline._annotated_side[510, 215, 0] < pipeline._annotated_side[510, 215, 1]


@pytest.mark.parametrize("scenario", ["shared", "separate", "preview-only", "missing"])
async def test_top_reference_merge_handles_slightly_overlapping_boxes(scenario, tmp_path):
    reference = np.full((400, 640, 3), 220, np.uint8)
    path = tmp_path / "empty-top.png"
    if scenario != "missing":
        assert cv2.imwrite(str(path), reference)
    settings = Settings(_env_file=None, detector={
        "reference_top": path, "top_reference_merge": scenario != "preview-only",
    })
    pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
    frame = reference.copy()
    if scenario != "separate":
        pipeline._track_roi(frame, "top")[120:170, 185:205, 0] -= 9
    original = frame.copy()
    boxes = [(100, 100, 200, 200), (190, 105, 290, 205)]

    def detect(image):
        np.testing.assert_array_equal(image, pipeline._track_roi(original, "top"))
        return [SimpleNamespace(bbox=box, conf=0.9) for box in boxes]

    pipeline._detector = ParcelTracker(settings.detector, SimpleNamespace(process=detect))
    await pipeline._process_track_frame({"top": frame})
    assert len(pipeline.overlays) == (1 if scenario == "shared" else 2)
    if scenario == "shared":
        overlay, = pipeline.overlays.values()
        assert (overlay.x1, overlay.y1, overlay.x2, overlay.y2) == (100, 100, 290, 205)
    np.testing.assert_array_equal(frame, original)
