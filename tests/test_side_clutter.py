from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.config import Settings
from app.detector import ParcelTracker
from app.pipeline import Pipeline


@pytest.mark.asyncio
async def test_side_clutter_is_per_frame_and_preview_only(monkeypatch, caplog):
    settings = Settings(_env_file=None)
    frame = np.full((400, 400, 3), 220, np.uint8)
    original = frame.copy()
    nested = [(50, 20, 180, 120), (60, 30, 170, 110), (70, 40, 160, 100)]
    separate = [(50, 20, 90, 100), (110, 20, 150, 100), (170, 20, 210, 100)]
    current = []

    def backend():
        return SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9, observed=observed)
                                                   for b, observed in current])

    tracker = ParcelTracker(settings.detector, backend(), side=True)
    control = ParcelTracker(settings.detector, backend(), side=True)
    pipeline = Pipeline(settings, MagicMock(), MagicMock(), MagicMock())
    pipeline._side_detector = tracker
    chips = []
    original_chip = pipeline._label_chip

    def chip(image, text, x, y, color):
        chips.append(text)
        original_chip(image, text, x, y, color)

    monkeypatch.setattr(pipeline, "_label_chip", chip)
    roi = pipeline._track_roi(frame, "side")
    for boxes, observed, expected in [(nested, True, True), ([(x1 + 3, y1, x2, y2) for x1, y1, x2, y2 in nested], True, True),
                                      (separate, True, False), (nested, False, False),
                                      (nested[:2], True, False)]:
        current[:] = [(b, observed) for b in boxes]
        expected_detections = control.process(roi)
        raw = []
        # Compare an independent tracker with and without the observation output.
        comparison = ParcelTracker(settings.detector, backend(), side=True)
        comparison_control = ParcelTracker(settings.detector, backend(), side=True)
        assert comparison.process(roi, raw_boxes=raw) == comparison_control.process(roi)
        chips.clear()
        await pipeline._process_side_track_frame(frame)
        assert ("candidate CLUTTER/perlu verifikasi" in chips) is expected
        assert sorted((s["bbox"], s["misses"]) for s in tracker._tracks.values()) == sorted(
            (s["bbox"], s["misses"]) for s in control._tracks.values())
        if expected:
            assert len(expected_detections) == 1  # raw boxes were merged

    assert len(pipeline._pending_side) == 1
    capture = pipeline._pending_side[0]["side"]
    np.testing.assert_array_equal(capture, roi[5:136, 34:200])
    np.testing.assert_array_equal(frame, original)
    assert "CLUTTER" not in caplog.text
