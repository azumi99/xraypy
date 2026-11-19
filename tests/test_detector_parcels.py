from types import SimpleNamespace
import logging

import numpy as np
import pytest

from app.config import DetectorConfig, Settings
from app.detector import (
    ParcelTracker,
    _merge_adjacent_boxes,
    _merge_bottom_fragments,
    _merge_reference_fragments,
    cluster_boxes,
)


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


@pytest.mark.parametrize("slices", [(slice(100, 112), slice(100, 240)), (slice(70, 210), slice(100, 112))])
def test_thin_dense_blob_is_detected(slices):
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[slices] = 70
    result = tracker([]).process(frame)
    assert len(result) == 1
    x1, y1, x2, y2 = result[0].bbox
    assert x1 <= slices[1].start and x2 >= slices[1].stop - 1
    assert y1 <= slices[0].start and y2 >= slices[0].stop - 1


def test_small_thin_blob_is_not_detected():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[100:106, 100:180] = 70
    assert tracker([]).process(frame) == []


def test_pale_blob_on_near_white_belt_is_not_lost():
    frame = np.full((360, 640, 3), 250, np.uint8)
    frame[90:210, 100:290] = 235
    boxes = [(110, 100, 175, 195), (205, 100, 280, 195)]
    assert [d.bbox for d in tracker(boxes).process(frame)] == [(100, 90, 290, 210)]


@pytest.mark.parametrize("side", [False, True])
def test_bottom_fragment_merges_before_ids_in_both_views(side, caplog):
    boxes = [(100, 80, 260, 200), (110, 204, 250, 216), (400, 80, 500, 180)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    t = ParcelTracker(DetectorConfig(merge_diagnostics=True), backend, side=side)
    frame = np.full((360, 640, 3), 220, np.uint8)
    with caplog.at_level(logging.INFO, logger="app.detector"):
        first, second = t.process(frame), t.process(frame)
    assert sorted(d.bbox for d in first) == [(100, 80, 260, 216), (400, 80, 500, 180)]
    assert {d.parcel_id for d in first} == {d.parcel_id for d in second}
    assert f"bottom_fragment_merge view={'side' if side else 'top'}" in caplog.text


@pytest.mark.parametrize("boxes", [
    [(110, 204, 250, 216)],  # genuine standalone thin parcel
    [(100, 80, 260, 200), (110, 207, 250, 219)],  # gap too large
    [(100, 80, 260, 200), (230, 204, 370, 216)],  # insufficient overlap
    [(100, 80, 260, 200), (110, 160, 250, 172)],  # not at the bottom
    [(100, 80, 260, 200), (110, 204, 250, 250)],  # not thin
    [(100, 80, 260, 200), (120, 70, 280, 200), (125, 204, 255, 216)],
    [(100, 80, 260, 200), (110, 203, 250, 215), (110, 205, 250, 217)],
])
def test_bottom_fragment_requires_unambiguous_geometry(boxes):
    assert _merge_bottom_fragments(boxes, 360) == boxes


def test_bottom_fragment_gap_scales_with_frame_height():
    boxes = [(200, 160, 520, 400), (220, 412, 500, 436)]
    assert _merge_bottom_fragments(boxes, 720) == [(200, 160, 520, 436)]
    boxes[1] = (220, 414, 500, 438)
    assert _merge_bottom_fragments(boxes, 720) == boxes


def test_side_overhanging_fragment_does_not_widen_box_or_absorb_neighbor():
    parent = (65, 110, 198, 245)
    neighbor = (232, 72, 334, 267)
    fragment = (65, 239, 240, 268)
    boxes = [parent, neighbor, fragment]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    frame = np.full((300, 428, 3), 220, np.uint8)
    side = ParcelTracker(DetectorConfig(), backend, side=True)
    assert sorted(d.bbox for d in side.process(frame)) == [(65, 110, 198, 268), neighbor]
    assert _merge_bottom_fragments(boxes, 300) == boxes
    ambiguous = boxes + [(62, 100, 200, 245)]
    assert _merge_bottom_fragments(ambiguous, 300, side=True) == ambiguous


@pytest.mark.parametrize("right_top", [72, 110])
def test_side_reference_merges_broad_bridge_with_aligned_or_mismatched_heights(right_top):
    boxes = [(232, right_top, 334, 267), (65, 110, 198, 268)]
    delta = np.zeros((300, 428), np.uint8)
    delta[130:190, 199:232] = 9
    delta[130:190, 199:202] = 22  # strong edge within a mostly weak seam
    assert _merge_reference_fragments(boxes, delta, 6, 18) == [(65, right_top, 334, 268)]


@pytest.mark.parametrize("scene", ["base-only", "separate", "broken", "sparse", "too-far", "chain", "intruder"])
def test_side_reference_rejects_inadequate_or_ambiguous_bridges(scene):
    boxes = [(65, 110, 198, 268), (232, 72, 334, 267)]
    delta = np.zeros((300, 640), np.uint8)
    if scene == "base-only":
        delta[250:268, 199:232] = 9
    elif scene != "separate":
        delta[130:190, 199:232] = 9
    if scene == "broken":
        delta[130:190, 215] = 0
    elif scene == "sparse":
        delta[131:190:2, 199:232] = 0
    elif scene == "too-far":
        boxes[1] = (260, 72, 380, 267)
        delta[130:190, 232:260] = 9
    elif scene == "chain":
        boxes.append((355, 96, 468, 258))
        delta[120:196, 335:355] = 9
    elif scene == "intruder":
        boxes.append((207, 125, 226, 195))
    assert _merge_reference_fragments(boxes, delta, 6, 18) == boxes


def test_blob_inside_larger_backend_box_is_not_duplicated():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[120:160, 140:200] = 70
    result = tracker([(100, 80, 260, 220)]).process(frame)
    assert len(result) == 1
    assert result[0].bbox == (100, 80, 260, 220)


def test_duplicate_backend_boxes_are_grouped_but_nearby_boxes_are_not():
    frame = np.full((360, 640, 3), 220, np.uint8)
    result = tracker([(50, 80, 150, 180), (52, 82, 148, 178),
                      (160, 80, 260, 180)]).process(frame)
    assert sorted(d.bbox for d in result) == [(50, 80, 150, 180), (160, 80, 260, 180)]


def test_nested_detections_group_before_adjacent_pairs_in_both_views():
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(100, 100, 125, 135), (105, 105, 120, 130),
             (128, 100, 153, 135), (132, 105, 148, 130),
             (250, 100, 275, 135), (278, 100, 303, 135),
             (450, 100, 500, 150)]
    for side in (False, True):
        backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
        t = ParcelTracker(DetectorConfig(), backend, side=side)
        first, second = t.process(frame), t.process(frame)
        assert sorted(d.bbox for d in first) == [(100, 100, 153, 135),
                                                  (250, 100, 303, 135),
                                                  (450, 100, 500, 150)]
        assert {d.parcel_id for d in first} == {d.parcel_id for d in second}


def test_duplicate_cluster_does_not_chain_beyond_outer_detection():
    boxes = [(0, 0, 100, 100), (9, 0, 109, 100), (18, 0, 118, 100)]
    assert cluster_boxes(boxes) == [(0, 0, 109, 100), (18, 0, 118, 100)]


def test_grouped_side_detection_persists_briefly_without_observation():
    boxes = [(100, 100, 125, 135), (105, 105, 120, 130), (128, 100, 153, 135)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    side = ParcelTracker(DetectorConfig(), backend, side=True)
    frame = np.full((360, 640, 3), 220, np.uint8)
    first = side.process(frame)
    boxes.clear()
    missed = side.process(frame)
    assert len(first) == len(missed) == 1
    assert missed[0].bbox == first[0].bbox == (100, 100, 153, 135)
    assert missed[0].parcel_id == first[0].parcel_id and not missed[0].observed


def test_partially_overlapping_backend_boxes_stay_separate():
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(50, 80, 150, 180), (130, 80, 230, 180)]
    assert sorted(d.bbox for d in tracker(boxes).process(frame)) == boxes


@pytest.mark.parametrize("gap", [0, 1, 3, 6])
def test_adjacent_backend_fragments_merge_across_frames(gap):
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(100, 100, 125, 135), (125 + gap, 100, 150 + gap, 135)]
    x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    frame[y1:y2 + 1, x1:x2 + 1] = 70
    t = tracker(boxes)
    first = t.process(frame)
    second = t.process(frame)
    assert len(first) == len(second) == 1
    assert first[0].bbox == (x1, y1, x2, y2)
    assert first[0].parcel_id == second[0].parcel_id


def test_adjacent_backend_boxes_merge_without_dark_seam():
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(100, 100, 125, 135), (128, 100, 153, 135)]
    frame[100:136, 100:125] = 70
    frame[100:136, 129:154] = 70
    assert [d.bbox for d in tracker(boxes).process(frame)] == [(100, 100, 153, 135)]


def test_slight_overlap_from_scanner_diagnostics_merges():
    gray = np.full((398, 720), 220, np.uint8)
    boxes = [(252, 168, 358, 258), (141, 150, 254, 263)]
    assert _merge_adjacent_boxes(boxes, gray, 190) == [(141, 150, 358, 263)]


def test_adjacent_blobs_and_stable_id():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[80:180, 100:170] = 70
    frame[80:180, 172:242] = 70
    t = tracker([])
    first, second = t.process(frame), t.process(frame)
    assert len(first) == len(second) == 1
    assert first[0].bbox[0] <= 100 and first[0].bbox[2] >= 241
    assert first[0].parcel_id == second[0].parcel_id


def test_adjacent_blob_and_backend_box_merge_before_tracking():
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[80:150, 100:160] = 70
    t = tracker([(166, 80, 226, 150)])
    first, second = t.process(frame), t.process(frame)
    assert len(first) == len(second) == 1
    assert first[0].bbox == (100, 80, 226, 150)
    assert first[0].parcel_id == second[0].parcel_id


def test_merge_rejects_long_gap_misalignment_and_ambiguous_chain():
    gray = np.full((360, 640), 70, np.uint8)
    for boxes in [
        [(100, 100, 125, 135), (132, 100, 157, 135)],
        [(100, 100, 125, 135), (125, 110, 150, 145)],
        [(100, 100, 125, 135), (100, 135, 125, 170)],
        [(100, 100, 125, 135), (125, 100, 180, 135)],
        [(100, 100, 125, 135), (125, 100, 150, 135), (150, 100, 175, 135)],
    ]:
        assert _merge_adjacent_boxes(boxes, gray, 150) == boxes


def test_top_merge_survives_two_borderline_frames_without_renewing():
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(100, 100, 200, 200), (205, 105, 305, 205)]
    t = tracker(boxes)
    first, = t.process(frame)
    boxes[1] = (210, 105, 310, 205)
    assert len(tracker(boxes).process(frame)) == 2
    for _ in range(2):
        current, = t.process(frame)
        assert current.parcel_id == first.parcel_id
    assert len(t.process(frame)) == 2


def test_recent_track_keeps_id_when_stale_track_also_matches():
    frame = np.full((360, 640, 3), 220, np.uint8)
    boxes = [(100, 100, 190, 190), (200, 100, 290, 190)]
    t = tracker(boxes)
    active = max(t.process(frame), key=lambda d: d.bbox[0])
    boxes.pop(0)
    t.process(frame)
    boxes[:] = [(150, 100, 240, 190)]
    result, = t.process(frame)
    assert result.parcel_id == active.parcel_id


@pytest.mark.parametrize("wrapper", ["shared", "separate", "thin_bridge"])
@pytest.mark.parametrize("side", [False, True])
def test_wrapper_uses_faint_raw_edges_before_assigning_ids(wrapper, side):
    frame = np.full((360, 640, 3), 220, np.uint8)
    frame[90:210, 90:185, 0] = 211
    frame[90:210, 195:290, 0] = 211
    if wrapper == "shared":
        frame[90:210, 185:195, 0] = 211
    elif wrapper == "thin_bridge":
        frame[205:207, 185:195, 0] = 211
    boxes = [(100, 100, 175, 202), (200, 100, 280, 202)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    t = ParcelTracker(DetectorConfig(), backend, side=side)
    if side:
        frame = frame[:215]  # Wrapper is fully visible, but the analysis window is clipped.
    result = t.process(frame)
    if wrapper == "shared":
        assert [d.bbox for d in result] == [(90, 90, 289, 209)]
        assert [d.parcel_id for d in t.process(frame)] == [result[0].parcel_id]
    else:
        assert sorted(d.bbox for d in result) == boxes


def test_side_wrapper_cut_by_roi_is_not_merged():
    frame = np.full((205, 640, 3), 220, np.uint8)
    frame[90:, 90:290, 0] = 211
    boxes = [(100, 100, 175, 195), (200, 100, 280, 195)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    result = ParcelTracker(DetectorConfig(), backend, side=True).process(frame)
    assert sorted(d.bbox for d in result) == boxes


def test_merge_gap_is_normalized_to_frame_size():
    gray = np.full((720, 1280), 70, np.uint8)
    boxes = [(200, 200, 250, 270), (262, 200, 312, 270)]
    assert _merge_adjacent_boxes(boxes, gray, 150) == [(200, 200, 312, 270)]
    boxes[1] = (264, 200, 314, 270)
    assert _merge_adjacent_boxes(boxes, gray, 150) == boxes


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


@pytest.mark.parametrize("side", [False, True])
def test_tracker_holds_unobserved_boxes_briefly(side):
    boxes = [(40, 20, 100, 80)]
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    top = ParcelTracker(DetectorConfig(), backend, side=side)
    frame = np.full((100, 400, 3), 220, np.uint8)
    assert top.process(frame)[0].observed
    boxes.clear()
    assert not top.process(frame)[0].observed
    assert not top.process(frame)[0].observed
    assert top.process(frame) == []


@pytest.mark.parametrize("boxes,pixel,reason", [
    ([(100, 100, 125, 135), (128, 100, 153, 135)], 70, "decision=MERGE"),
    ([(100, 100, 125, 135), (132, 100, 157, 135)], 70, "decision=SKIP:geometry"),
    ([(100, 100, 125, 135), (128, 100, 153, 135)], 220, "decision=MERGE"),
    ([(100, 100, 125, 135), (125, 100, 150, 135), (150, 100, 175, 135)], 70, "decision=SKIP:ambiguous_neighbors"),
])
def test_merge_diagnostics_do_not_change_decisions(boxes, pixel, reason, caplog):
    gray = np.full((360, 640), pixel, np.uint8)
    expected = _merge_adjacent_boxes(boxes, gray, 150)
    with caplog.at_level(logging.INFO, logger="app.detector"):
        assert _merge_adjacent_boxes(boxes, gray, 150, diagnostic_view="top") == expected
    assert reason in caplog.text
    assert "view=top resolution=640x360" in caplog.text
    assert "dino_similarity=unavailable" in caplog.text
    assert "normalized_x_gap=" in caplog.text and "normalized_vertical_overlap=" in caplog.text
    assert "union_px=" in caplog.text
    if pixel == 220 and reason == "decision=MERGE":
        assert "dark_ratio=0.0000" in caplog.text


def test_diagnostics_count_candidates_beyond_log_limit(caplog):
    gray = np.full((360, 640), 220, np.uint8)
    boxes = [(100, 100, 125, 135), (125, 100, 150, 135)]
    boxes += [(300 + i * 20, 200, 310 + i * 20, 220) for i in range(20)]
    boxes.append((150, 100, 175, 135))
    with caplog.at_level(logging.INFO, logger="app.detector"):
        result = _merge_adjacent_boxes(boxes, gray, 150, diagnostic_view="top")
    assert boxes[0] in result and boxes[1] in result and boxes[-1] in result
    assert "decision=SKIP:ambiguous_neighbors" in caplog.records[0].message
    assert len(caplog.records) == 20


def test_merge_diagnostics_are_opt_in_bounded_and_rate_limited(caplog, monkeypatch):
    boxes = [(i * 15, 20, i * 15 + 10, 40) for i in range(10)]
    frame = np.full((100, 400, 3), 220, np.uint8)
    monkeypatch.setattr("app.detector.time.monotonic", lambda: 100.0)
    backend = SimpleNamespace(process=lambda _: [SimpleNamespace(bbox=b, conf=0.9) for b in boxes])
    with caplog.at_level(logging.INFO, logger="app.detector"):
        ParcelTracker(DetectorConfig(), backend).process(frame)
        assert not caplog.records
        side = ParcelTracker(DetectorConfig(merge_diagnostics=True), backend, side=True)
        side.process(frame)
        assert len(caplog.records) == 20
        assert all("view=side" in r.message for r in caplog.records)
        side.process(frame)
        assert len(caplog.records) == 20


def test_merge_diagnostics_environment(monkeypatch):
    monkeypatch.setenv("CV_DETECTOR__MERGE_DIAGNOSTICS", "true")
    assert Settings().detector.merge_diagnostics
