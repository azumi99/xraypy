from types import SimpleNamespace

import numpy as np
import pytest

from app.config import DetectorConfig, Settings
from app.detector import ParcelTracker


def tracker(boxes, **config):
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    return ParcelTracker(DetectorConfig(**config), backend)


def test_enclosing_blob_wins_over_high_iou_content():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[80:240, 100:260] = 70
    result = tracker([(120, 100, 240, 220)]).process(frame)
    assert len(result) == 1
    x1, y1, x2, y2 = result[0].bbox
    assert x1 <= 100 and y1 <= 80 and x2 >= 259 and y2 >= 239


def test_nearby_blobs_stay_separate_with_content_boxes():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[80:220, 50:150] = 70
    frame[80:220, 165:265] = 70
    result = tracker([(60, 90, 140, 210), (175, 90, 255, 210)]).process(frame)
    assert len(result) == 2
    boxes = sorted(d.bbox for d in result)
    assert boxes[0][0] <= 50 and boxes[0][2] >= 149
    assert boxes[1][0] <= 165 and boxes[1][2] >= 264
    assert boxes[0][2] < boxes[1][0]


def test_configured_bottom_clips_yolo_and_blobs():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[80:260, 100:260] = 70
    frame[270:] = 40
    result = tracker([(120, 100, 240, 300), (400, 280, 500, 340)], viewport_bottom=0.75).process(frame)
    assert result
    assert all(d.bbox[3] < 270 for d in result)
    assert all(d.bbox[0] < 400 for d in result)


def test_boundary_environment_and_validation(monkeypatch):
    monkeypatch.setenv("CV_DETECTOR__VIEWPORT_BOTTOM", "0.8")
    assert Settings().detector.viewport_bottom == 0.8
    with pytest.raises(ValueError):
        DetectorConfig(viewport_bottom=1.1)


def test_side_persistence_does_not_duplicate_overlapping_observations():
    boxes = [(40, 20, 100, 80), (130, 20, 190, 80)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    side = ParcelTracker(DetectorConfig(), backend, side=True)
    frame = np.full((100, 400, 3), 220, np.uint8)
    assert len(side.process(frame)) == 2
    boxes[:] = [(80, 20, 150, 80)]
    result = side.process(frame)
    assert len(result) == 1
    assert result[0].observed


def test_top_tracker_still_returns_only_current_boxes():
    boxes = [(40, 20, 100, 80)]
    top = tracker(boxes)
    frame = np.full((100, 400, 3), 220, np.uint8)
    assert top.process(frame)[0].observed
    boxes.clear()
    assert top.process(frame) == []
