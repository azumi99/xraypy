"""Parcel detection + tracking.

Two backends behind one interface:
- ``yolo``:   Ultralytics YOLO12-seg (terbaru) + ByteTrack (model.track,
              persist=True). Class-agnostic default ("all") supaya objek apa
              pun di feed X-ray dihitung parcel; untuk akurasi produksi,
              fine-tune model parcel X-ray lalu set CV_DETECTOR__WEIGHTS +
              CV_DETECTOR__CLASSES=parcel.
- ``motion``: MOG2 background subtraction + centroid tracker. Works well for
              conveyor X-ray feeds with a static belt; the default so the
              service runs out of the box.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from itertools import groupby

import cv2
import numpy as np

from .config import DetectorConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Detection:
    track_id: int
    mask: np.ndarray  # uint8 binary mask, frame-sized
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 (tight, frame coords)
    conf: float = 0.0
    cls: int = -1
    observed: bool = True


@dataclass
class _TrackState:
    mask: np.ndarray
    centroid: tuple[float, float]
    missed: int = 0
    hits: int = 1


from abc import ABC, abstractmethod  # noqa: E402


class DetectorBackend(ABC):
    @abstractmethod
    def process(self, frame_bgr: np.ndarray) -> list[Detection]:
        """Return tracked detections for one frame."""

    @abstractmethod
    def reset(self) -> None: ...


class YoloSegBackend(DetectorBackend):
    """YOLO12-seg + ByteTrack via ultralytics.track."""

    def __init__(self, cfg: DetectorConfig) -> None:
        from ultralytics import YOLO

        self._cfg = cfg
        try:
            self._model = YOLO(cfg.weights)
        except Exception as exc:
            raise RuntimeError(
                f"failed to load YOLO weights {cfg.weights!r}: {exc}. "
                "Pre-download with `.venv/bin/python tools/preload_models.py`."
            ) from exc
        self._cls_ids = self._resolve_classes(cfg.classes, self._model.names or {})

    @staticmethod
    def _resolve_classes(spec: str, names: dict[int, str]) -> list[int] | None:
        """'all' -> None (semua kelas); 'suitcase,backpack' / '0,24' -> ids."""
        spec = (spec or "all").strip()
        if not spec or spec.lower() == "all":
            return None
        name_to_id = {v: k for k, v in names.items()}
        ids: list[int] = []
        for c in (x.strip() for x in spec.split(",")):
            if c.isdigit():
                ids.append(int(c))
            elif c in name_to_id:
                ids.append(name_to_id[c])
            else:
                raise ValueError(f"kelas {c!r} tidak ada di model (tersedia: {sorted(set(names.values()))})")
        return ids

    def process(self, frame_bgr: np.ndarray) -> list[Detection]:
        results = self._model.track(
            frame_bgr,
            persist=True,
            conf=self._cfg.conf,
            iou=self._cfg.iou,
            classes=self._cls_ids,
            tracker="bytetrack.yaml",
            verbose=False,
        )
        detections: list[Detection] = []
        if not results:
            return detections
        r = results[0]
        if r.boxes is None or r.boxes.id is None:
            return detections
        h, w = frame_bgr.shape[:2]
        for i, tid in enumerate(r.boxes.id.int().tolist()):
            xyxy = r.boxes.xyxy[i].cpu().numpy()
            x1, y1 = max(0, int(xyxy[0])), max(0, int(xyxy[1]))
            x2, y2 = min(w - 1, int(xyxy[2])), min(h - 1, int(xyxy[3]))
            mask = np.zeros((h, w), dtype=np.uint8)
            if r.masks is not None:
                m = r.masks.data[i].cpu().numpy().astype(np.uint8)
                if m.shape != (h, w):
                    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
                mask = (m > 0.5).astype(np.uint8)
            else:
                mask[y1 : y2 + 1, x1 : x2 + 1] = 1
            detections.append(
                Detection(
                    track_id=tid,
                    mask=mask,
                    bbox=(x1, y1, x2, y2),
                    conf=float(r.boxes.conf[i]),
                    cls=int(r.boxes.cls[i]),
                )
            )
        return detections

    def reset(self) -> None:
        # persist=True keeps state; a fresh session simply continues tracking ids
        pass


def _bbox_of(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return 0, 0, mask.shape[1] - 1, mask.shape[0] - 1
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


class MotionBackend(DetectorBackend):
    """MOG2 background subtraction + nearest-centroid tracker."""

    def __init__(self, cfg: DetectorConfig) -> None:
        self._cfg = cfg
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=cfg.bg_history, varThreshold=25, detectShadows=False
        )
        self._tracks: dict[int, _TrackState] = {}
        self._next_id = 1

    def reset(self) -> None:
        self._tracks.clear()

    def process(self, frame_bgr: np.ndarray) -> list[Detection]:
        fg = self._bg.apply(frame_bgr)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        centroids: list[tuple[float, float]] = []
        masks: list[np.ndarray] = []
        for c in contours:
            if cv2.contourArea(c) < self._cfg.min_area_px:
                continue
            m = np.zeros(frame_bgr.shape[:2], dtype=np.uint8)
            cv2.drawContours(m, [c], -1, 1, -1)
            M = cv2.moments(c)
            if M["m00"] == 0:
                continue
            centroids.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))
            masks.append(m)

        # associate centroids to existing tracks (greedy nearest)
        assigned: dict[int, int] = {}
        used = set()
        for tid, state in self._tracks.items():
            best, best_d = None, 1e18
            for i, c in enumerate(centroids):
                if i in used:
                    continue
                d = (state.centroid[0] - c[0]) ** 2 + (state.centroid[1] - c[1]) ** 2
                if d < best_d:
                    best, best_d = i, d
            if best is not None and best_d < 250**2:
                assigned[tid] = best
                used.add(best)

        outputs: list[Detection] = []
        for tid, i in assigned.items():
            state = self._tracks[tid]
            state.mask = masks[i]
            state.centroid = centroids[i]
            state.missed = 0
            state.hits += 1
            outputs.append(Detection(track_id=tid, mask=state.mask, bbox=_bbox_of(state.mask)))
        for tid in list(self._tracks.keys()):
            if tid not in assigned:
                self._tracks[tid].missed += 1
                if self._tracks[tid].missed <= 2 and self._tracks[tid].hits >= 3:
                    outputs.append(Detection(track_id=tid, mask=self._tracks[tid].mask, bbox=_bbox_of(self._tracks[tid].mask), observed=False))
                if self._tracks[tid].missed > self._cfg.finalize_missed_frames:
                    del self._tracks[tid]
        for i, c in enumerate(centroids):
            if i not in used:
                tid = self._next_id
                self._next_id += 1
                self._tracks[tid] = _TrackState(mask=masks[i], centroid=c)
                outputs.append(Detection(track_id=tid, mask=masks[i], bbox=_bbox_of(masks[i])))
        return outputs

    def active_tracks(self) -> list[int]:
        return list(self._tracks.keys())


def make_detector(cfg: DetectorConfig) -> DetectorBackend:
    if cfg.backend == "yolo":
        logger.info("detector backend: yolo (%s) + ByteTrack", cfg.weights)
        return YoloSegBackend(cfg)
    logger.info("detector backend: motion (MOG2 + centroid tracker)")
    return MotionBackend(cfg)


# ---------------------------------------------------------------------------
# Parcel framing — kotak presisi ke PARCEL, bukan ke isinya.
#
# Pada citra X-ray, model umum mendeteksi isi paket (benda di dalam) sebagai
# objek terpisah. ParcelTracker membungkusnya menjadi kotak parcel utuh:
# 1. deteksi yang sangat overlap di-kluster -> satu parcel
# 2. tiap sisi kotak di-snap ke tepi paket (garis gradien terkuat, Scharr)
# 3. tracking (IoU) + id stabil dilakukan di level parcel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParcelDetection:
    parcel_id: int
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 — kotak parcel utuh
    conf: float = 0.0
    observed: bool = True


def _mostly_contained(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1])
    return area > 0 and intersection >= 0.9 * area


def cluster_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Group duplicates against a real outer box, never a transitive union."""
    remaining = sorted(boxes, key=lambda b: (-(b[2] - b[0]) * (b[3] - b[1]), b))
    result = []
    while remaining:
        anchor = remaining.pop(0)
        group = [anchor]
        rest = []
        for box in remaining:
            if _iou(*anchor, *box) >= 0.8 or _mostly_contained(box, anchor):
                group.append(box)
            else:
                rest.append(box)
        result.append((min(b[0] for b in group), min(b[1] for b in group),
                       max(b[2] for b in group), max(b[3] for b in group)))
        remaining = rest
    return result


def _merge_bottom_fragments(boxes: list[tuple[int, int, int, int]], height: int,
                            *, side: bool = False, diagnostic_view: str | None = None) -> list[tuple[int, int, int, int]]:
    """Join only a single thin base to a single, substantially taller parent."""
    candidates = []
    for i, fragment in enumerate(boxes):
        fw, fh = fragment[2] - fragment[0], fragment[3] - fragment[1]
        if fh > 0.25 * fw:
            continue
        for j, parent in enumerate(boxes):
            if i == j:
                continue
            pw, ph = parent[2] - parent[0], parent[3] - parent[1]
            overlap = min(fragment[2], parent[2]) - max(fragment[0], parent[0])
            dy = fragment[1] - parent[3]
            if fh > 0.25 * ph or dy * 360 > 6 * height or fragment[3] <= parent[3]:
                continue
            aligned = overlap >= 0.8 * fw and overlap >= 0.5 * pw and dy >= -min(fh, 0.15 * ph)
            # A side-view base may overhang slightly; keep the parent's horizontal bounds.
            overhang = side and fw > pw and overlap >= 0.75 * fw and overlap >= 0.9 * pw and dy >= -fh
            if aligned or overhang:
                candidates.append((i, j, overhang))
    counts = [sum(k in (i, j) for i, j, _ in candidates) for k in range(len(boxes))]
    used = set()
    result = []
    for i, j, overhang in candidates:
        if counts[i] != 1 or counts[j] != 1:
            continue
        a, b = boxes[i], boxes[j]
        result.append((b[0], b[1], b[2], a[3]) if overhang else
                      (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])))
        used.update((i, j))
        if diagnostic_view is not None:
            logger.info("bottom_fragment_merge view=%s normalized_gap=%.2f clipped_overhang=%s",
                        diagnostic_view, (a[1] - b[3]) * 360 / height, overhang)
    return result + [box for i, box in enumerate(boxes) if i not in used]


def _merge_adjacent_boxes(boxes: list[tuple[int, int, int, int]], gray: np.ndarray,
                          threshold: float, *, diagnostic_view: str | None = None,
                          recent_unions: tuple[tuple[int, int, int, int], ...] = (),
                          confirmed_unions: list[tuple[int, int, int, int]] | None = None) -> list[tuple[int, int, int, int]]:
    """Merge unambiguous horizontally adjacent, vertically aligned similar boxes.

    Distinct touching packages can satisfy this geometry and merge incorrectly.
    """
    h, w = gray.shape
    normalized = [(x1 * 640 / w, y1 * 360 / h, x2 * 640 / w, y2 * 360 / h)
                  for x1, y1, x2, y2 in boxes]
    candidates: list[tuple[int, int]] = []
    carried: set[tuple[int, int]] = set()
    diagnostics = []
    for i, a in enumerate(normalized):
        for j in range(i + 1, len(boxes)):
            b = normalized[j]
            details = [] if diagnostic_view is not None and len(diagnostics) < 20 else None
            for axis in (0,):
                cross = 1 - axis
                first, second = sorted((a, b), key=lambda box: box[axis])
                gap = second[axis] - first[axis + 2]
                overlap = min(a[cross + 2], b[cross + 2]) - max(a[cross], b[cross])
                cross_sizes = (a[cross + 2] - a[cross], b[cross + 2] - b[cross])
                sizes = (a[axis + 2] - a[axis], b[axis + 2] - b[axis])
                similar_width = min(sizes) >= 0.5 * max(sizes)
                strict = abs(gap) <= 6 and overlap >= 0.75 * max(cross_sizes) and similar_width
                stable = False
                if not strict and recent_unions and abs(gap) <= 12 and similar_width and overlap >= 0.65 * max(cross_sizes):
                    p, q = boxes[i], boxes[j]
                    union = (min(p[0], q[0]), min(p[1], q[1]), max(p[2], q[2]), max(p[3], q[3]))
                    stable = (any(_iou(*union, *previous) >= 0.8 for previous in recent_unions)
                              and not any(k not in (i, j) and _iou(*union, *box) > 0
                                          for k, box in enumerate(boxes)))
                if not (strict or stable):
                    if details is not None:
                        reasons = []
                        if not -6 <= gap <= 6:
                            reasons.append("gap")
                        if not overlap >= 0.75 * max(cross_sizes):
                            reasons.append("cross_overlap")
                        if not min(sizes) >= 0.5 * max(sizes):
                            reasons.append("size_ratio")
                        details.append(f"{'xy'[axis]}:geometry({','.join(reasons)})")
                    continue
                candidates.append((i, j))
                if stable:
                    carried.add((i, j))
                if details is not None:
                    # Observation only: seam brightness does not affect merging.
                    scale = (w / 640, h / 360)
                    lo = int((max(a[cross], b[cross]) + 0.2 * overlap) * scale[cross])
                    hi = int((min(a[cross + 2], b[cross + 2]) - 0.2 * overlap) * scale[cross])
                    start = max(0, int((first[axis + 2] - 2) * scale[axis]))
                    end = min(w, int(np.ceil((second[axis] + 2) * scale[axis])) + 1)
                    seam = gray[lo:hi, start:end]
                    dark_ratio = np.mean(seam < threshold) if seam.size else None
                    details.append(f"{'xy'[axis]}:dark_ratio={float(dark_ratio):.4f}" if dark_ratio is not None else f"{'xy'[axis]}:empty_seam")
                break
            if details is not None:
                diagnostics.append((i, j, details, dark_ratio if details and ':dark_ratio=' in details[-1] else None))
    # Ambiguous neighbors are left separate rather than forming transitive chains.
    counts = [sum(i in pair for pair in candidates) for i in range(len(boxes))]
    for i, j, details, dark_ratio in diagnostics:
        a, b = boxes[i], boxes[j]
        na, nb = normalized[i], normalized[j]
        xgap = max(na[0], nb[0]) - min(na[2], nb[2])
        ygap = max(na[1], nb[1]) - min(na[3], nb[3])
        vertical_overlap = max(0, min(na[3], nb[3]) - max(na[1], nb[1]))
        union = (max(a[2], b[2]) - min(a[0], b[0]), max(a[3], b[3]) - min(a[1], b[1]))
        if (i, j) in candidates:
            decision = "SKIP:ambiguous_neighbors"
            if counts[i] == counts[j] == 1:
                decision = "MERGE:recent_union" if (i, j) in carried else "MERGE"
        else:
            decision = "SKIP:geometry"
        logger.info(
            "merge_diagnostic view=%s resolution=%sx%s boxes=%s,%s normalized_x_gap=%.2f "
            "normalized_y_gap=%.2f normalized_vertical_overlap=%.2f dark_ratio=%s "
            "threshold=%.2f dino_similarity=unavailable union_px=%s decision=%s checks=%s",
            diagnostic_view, w, h, a, b, xgap, ygap, vertical_overlap,
            f"{dark_ratio:.4f}" if dark_ratio is not None else "unavailable", threshold,
            union, decision, ";".join(details),
        )
    used: set[int] = set()
    result = []
    for i, j in candidates:
        if counts[i] == counts[j] == 1:
            a, b = boxes[i], boxes[j]
            union = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
            result.append(union)
            if confirmed_unions is not None and (i, j) not in carried:
                confirmed_unions.append(union)
            used.update((i, j))
    return result + [box for i, box in enumerate(boxes) if i not in used]


def _merge_reference_fragments(
    boxes: list[tuple[int, int, int, int]], delta: np.ndarray, weak: int, strong: int,
    *, side: bool = True, diagnostic_view: str | None = None,
) -> list[tuple[int, int, int, int]]:
    """Join isolated fragments only across a broad above-base reference bridge."""
    h, w = delta.shape
    candidates = []
    counts = [0] * len(boxes)
    # ponytail: pairwise scan over parcel boxes; index by x if crowded frames make this costly.
    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            left, right = (a, b) if a[0] < b[0] else (b, a)
            start, end = left[2] + 1, right[0]
            gap = end - start
            narrower = min(a[2] - a[0], b[2] - b[0])
            if right[2] <= left[2] or not abs(gap) <= min(55 * w / 640, 0.4 * narrower):
                continue
            if gap <= 0:
                if side or abs(gap) > 20 * w / 640:
                    continue
                # Sample across the shared boundary, including exact contact.
                start, end = end - 2, start + 2
            top, bottom = max(a[1], b[1]), min(a[3], b[3]) + 1
            overlap = bottom - top
            ah, bh = a[3] - a[1] + 1, b[3] - b[1] + 1
            if overlap < max(0.65 * min(ah, bh), 0.5 * max(ah, bh)):
                continue
            # Exclude the lowest quarter of the overlap so a floor strip cannot supply the bridge.
            seam = delta[max(0, top):min(h, top + 3 * overlap // 4), max(0, start):min(w, end)]
            if seam.size == 0:
                continue
            spanning = (seam >= weak).all(axis=1) & ((seam < strong).mean(axis=1) >= 0.5)
            if not any(ok and sum(1 for _ in run) >= max(4, 0.30 * overlap) for ok, run in groupby(spanning)):
                continue
            union = (left[0], min(a[1], b[1]), right[2], max(a[3], b[3]))
            candidates.append((i, j, union))
            counts[i] += 1
            counts[j] += 1
    merged = {}
    used = set()
    for i, j, union in candidates:
        if counts[i] != 1 or counts[j] != 1:
            continue
        if any(k not in (i, j) and _iou(*box, *union) > 0 for k, box in enumerate(boxes)):
            continue
        merged[i] = union
        used.add(j)
        if diagnostic_view is not None:
            logger.info("%s_reference_merge boxes=%s,%s union=%s", diagnostic_view, boxes[i], boxes[j], union)
    return [merged.get(i, box) for i, box in enumerate(boxes) if i not in used]


def _wrapper_box(frame: np.ndarray, left: tuple[int, int, int, int],
                 right: tuple[int, int, int, int], weak: int, *, side: bool = False) -> tuple[int, int, int, int] | None:
    h, w = frame.shape[:2]
    x1, y1 = min(left[0], right[0]), min(left[1], right[1])
    x2, y2 = max(left[2], right[2]), max(left[3], right[3])
    padx, pady = max(6, round(24 * w / 640)), max(6, round(24 * h / 360))
    x0, y0 = max(0, x1 - padx), max(0, y1 - pady)
    if x0 >= x1 or y0 >= y1 or x2 >= w - 1 or (not side and y2 >= h - 1):
        return None
    bottom_clipped = side and y2 >= h - 1
    if side:
        x0, y0 = 0, 0
        patch = frame.astype(np.int16)
        # Parcels rest on the belt covering the bottom rows: estimate the belt
        # background from the empty tunnel rows at the top of the side ROI.
        border = patch[: max(3, h // 20)].reshape(-1, 3)
    else:
        patch = frame[y0:y2 + pady + 1, x0:x2 + padx + 1].astype(np.int16)
        border = np.concatenate((patch[0], patch[-1], patch[:, 0], patch[:, -1]))
    background = np.median(border, axis=0)
    if np.percentile(np.abs(border - background).max(axis=1), 90) >= weak:
        return None
    contrast = (background - patch).max(axis=2)
    # Suppress stronger background gradients if the faint mask reaches the viewport edge.
    for threshold in ((weak, 2 * weak) if side else (weak,)):
        mask = (contrast >= threshold).astype(np.uint8)
        # Opening removes thin bridges without closing the gap between separate parcels.
        k = max(3, round(5 * w / 640) | 1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            if x == 0 or y == 0 or x + bw == patch.shape[1]:
                continue
            if side:
                # A belt-resting wrapper may touch the bottom; reject near-full floods.
                full_height = bh > 0.75 * h and not (y < 0.1 * h and y + bh == h)
                if bw > 0.92 * w or full_height:
                    continue
            elif y + bh == patch.shape[0]:
                continue
            outer = (x + x0, y + y0, x + x0 + bw - 1, y + y0 + bh - 1)
            if not (outer[0] < x1 and outer[1] < y1 and outer[2] > x2
                    and (outer[3] >= y2 if bottom_clipped else outer[3] > y2)):
                continue
            if all(cv2.pointPolygonTest(contour, ((a + c) / 2 - x0, (b + d) / 2 - y0), False) > 0
                   for a, b, c, d in (left, right)):
                return outer
    return None


def _merge_wrappers(boxes: list[tuple[int, int, int, int]], frame: np.ndarray, weak: int,
                    *, diagnostic_view: str | None = None, side: bool = False) -> list[tuple[int, int, int, int]]:
    w = frame.shape[1]
    proposals = []
    counts = [0] * len(boxes)
    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            left, right = sorted((a, boxes[j]), key=lambda box: box[0])
            if not side and (left[0] == right[0] or right[2] <= left[2]):
                continue
            gap = right[0] - left[2]
            if not side and abs(gap) > min(40 * w / 640, 0.35 * min(left[2] - left[0], right[2] - right[0])):
                continue
            overlap = min(left[3], right[3]) - max(left[1], right[1])
            if not side and overlap < 0.6 * max(left[3] - left[1], right[3] - right[1]):
                continue
            outer = _wrapper_box(frame, left, right, weak, side=side)
            if outer is None:
                continue
            members = tuple(k for k, box in enumerate(boxes) if _mostly_contained(box, outer))
            if not side and len(members) != 2:
                continue
            if any(k not in members and _iou(*outer, *box) > 0 for k, box in enumerate(boxes)):
                continue
            proposal = (members, outer)
            if proposal not in proposals:
                proposals.append(proposal)
                for k in members:
                    counts[k] += 1
    merged, used = {}, set()
    for members, outer in proposals:
        if all(counts[k] == 1 for k in members):
            i, *others = members
            merged[i] = outer
            used.update(others)
            if diagnostic_view is not None:
                logger.info("%s_wrapper_merge boxes=%s outer=%s", diagnostic_view, [boxes[k] for k in members], outer)
    return [merged.get(i, box) for i, box in enumerate(boxes) if i not in used]


def capture_silhouettes(frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find capture silhouettes, including components that still touch an edge."""
    h, w = frame.shape[:2]
    band = frame[:max(3, h // 20)].astype(np.int16)
    background = np.median(band, axis=0)
    if np.median(background) < 100:
        return []
    variation = np.percentile(np.abs(band - background).max(axis=2), 90)
    if variation > 20:
        return []
    contrast = (background - frame).max(axis=2)
    mask = (contrast >= max(12, round(2 * variation))).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    silhouettes = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width >= w * 0.025 and height >= h * 0.025 and cv2.contourArea(contour) >= width * height * 0.25:
            silhouettes.append((x, y, x + width - 1, y + height - 1))
    return silhouettes


def _side_flat_blobs(frame: np.ndarray, viewport_end: int) -> list[tuple[int, int, int, int]]:
    """Find long, low-profile parcels whose X-ray color survives weak grayscale contrast."""
    small = cv2.resize(frame, (640, 360))
    chroma = np.ptp(small, axis=2)
    background = np.median(chroma[3:14])
    mask = (chroma >= background + 14).astype(np.uint8)
    mask[viewport_end:] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blobs = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if (width < 80 or height < 8 or width < 4 * height or y < viewport_end // 2
                or width >= 0.95 * 640 or cv2.contourArea(contour) < 0.7 * width * height):
            continue
        # Ignore sparse pixels at the ends of the silhouette, including belt-edge noise.
        rows = np.flatnonzero(mask[y:y + height, x:x + width].sum(axis=1) >= 0.25 * width)
        if rows.size >= 8:
            blobs.append((x, y + int(rows[0]), width, int(rows[-1] - rows[0] + 1)))
    return blobs


def _snap_profile(strip: np.ndarray, min_ratio: float = 2.0) -> int | None:
    """Ambil indeks (axis-0 dari strip hasil transpose) dengan energi tepi
    terkuat — harus JELAS di atas rata-rata agar dianggap tepi paket nyata
    (bukan tepi layar/belt/isi yang lemah)."""
    if strip.size == 0:
        return None
    profile = np.abs(strip).sum(axis=1)
    i = int(profile.argmax())
    mean = float(profile.mean()) + 1e-3
    if profile[i] < min_ratio * mean:
        return None
    return i


def frame_parcel(
    gray: np.ndarray,
    bbox: tuple[int, int, int, int],
    max_expand: int | None = None,
    pad: float = 0.06,
) -> tuple[int, int, int, int]:
    """Perluas kotak deteksi sampai tepi parcel (garis tepi terkuat) di tiap
    sisi; jika tak ada tepi jelas, pakai padding relatif. Konservatif: batas
    ekspansi kecil + luas hasil dibatasi agar tidak melompat ke tepi layar."""
    h, w = gray.shape
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    if max_expand is None:
        max_expand = max(20, int(0.30 * max(bw, bh)))
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    rows = slice(max(0, y1), min(h - 1, y2) + 1)
    cols = slice(max(0, x1), min(w - 1, x2) + 1)

    # tepi kiri/kanan: energi gradien-x per KOLOM, dijumlahkan sepanjang rows
    def snap_x(lo: int, hi: int) -> int | None:
        i = _snap_profile(gx[rows, max(0, lo) : hi + 1].T)
        return max(0, lo) + i if i is not None else None

    # tepi atas/bawah: energi gradien-y per BARIS, dijumlahkan sepanjang cols
    def snap_y(lo: int, hi: int) -> int | None:
        i = _snap_profile(gy[max(0, lo) : hi + 1, cols])
        return max(0, lo) + i if i is not None else None

    left = snap_x(max(0, x1 - max_expand), x1)
    right = snap_x(x2, min(w - 1, x2 + max_expand))
    top = snap_y(max(0, y1 - max_expand), y1)
    bottom = snap_y(y2, min(h - 1, y2 + max_expand))

    px, py = max(4, int(bw * pad)), max(4, int(bh * pad))
    nx1 = left if left is not None else max(0, x1 - px)
    nx2 = right if right is not None else min(w - 1, x2 + px)
    ny1 = top if top is not None else max(0, y1 - py)
    ny2 = bottom if bottom is not None else min(h - 1, y2 + py)
    # jaga kewajaran: tak menyusut drastis / tak melebihi batas expand
    nx1 = int(np.clip(nx1, max(0, x1 - max_expand), x1))
    nx2 = int(np.clip(nx2, x2, min(w - 1, x2 + max_expand)))
    ny1 = int(np.clip(ny1, max(0, y1 - max_expand), y1))
    ny2 = int(np.clip(ny2, y2, min(h - 1, y2 + max_expand)))
    # pengaman: hasil framing tak boleh jauh lebih besar dari kluster isi
    # (mencegah lompat ke tepi layar/belt); bila melampaui -> padding saja
    area = (nx2 - nx1) * (ny2 - ny1)
    if area > 2.5 * max(1, bw * bh):
        return (
            max(0, x1 - px),
            max(0, y1 - py),
            min(w - 1, x2 + px),
            min(h - 1, y2 + py),
        )
    return nx1, ny1, nx2, ny2


class ParcelTracker:
    """Tracking level-parcel di atas backend deteksi apapun (yolo/motion)."""

    def __init__(self, cfg: DetectorConfig, backend: DetectorBackend, *, side: bool = False) -> None:
        self._cfg = cfg
        self._backend = backend
        self._side = side
        self._tracks: dict[int, dict] = {}
        self._next_pid = 1
        self._merge_diagnostic_at = float("-inf")
        self._recent_top_unions: list[tuple[tuple[int, int, int, int], ...]] = []

    def reset(self) -> None:
        self._tracks.clear()
        self._recent_top_unions.clear()
        self._next_pid = 1
        self._backend.reset()

    def process(self, frame_bgr: np.ndarray, *, reference_delta: np.ndarray | None = None,
                raw_boxes: list | None = None) -> list[ParcelDetection]:
        dets = self._backend.process(frame_bgr)
        if self._side:
            dets = [d for d in dets if getattr(d, "observed", True)]
            if raw_boxes is not None:
                raw_boxes.extend(d.bbox for d in dets)
        h, w = frame_bgr.shape[:2]
        bottom_limit = max(1, int(h * self._cfg.viewport_bottom)) if self._cfg.viewport_bottom is not None else h
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        boxes = [(x1, y1, x2, min(y2, bottom_limit - 1))
                 for d in dets for x1, y1, x2, y2 in [d.bbox]
                 if y1 < bottom_limit and x2 > x1 and min(y2, bottom_limit - 1) > y1]
        if self._side and self._cfg.reference_side is None and len(boxes) > 1:
            # Preserve the outer silhouette before dark blobs replace the inner detections.
            boxes = _merge_wrappers(boxes, frame_bgr[:bottom_limit], self._cfg.reference_weak_delta, side=True)
        # Reference scanner uses a bright belt: assist COCO YOLO with dark blobs.
        small = cv2.resize(gray, (640, 360))
        band = small[3:14]
        mean, std = float(band.mean()), float(band.std())
        bg = float(np.percentile(small, 90)) if mean < 140 or std > 28 else mean
        threshold = max(20, round(bg - max(14, 12 + std * 2)))
        mask = (small < threshold).astype(np.uint8) * 255
        rows = small.mean(axis=1)
        viewport = float(np.percentile(rows, 70))
        top, bottom = 0, 359
        while top < bottom and rows[top] < max(50, bg * 0.45):
            top += 1
        while bottom > top and rows[bottom] < viewport - 30:
            bottom -= 1
        if bottom - top > 360 * 0.45:
            mask[:top] = 0
            mask[bottom + 1:] = 0
        mask[int(bottom_limit * 360 / h):] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        gray_blobs = []
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            normal = bw >= 45 and bh >= 25 and bw * bh >= 3000
            thin = (min(bw, bh) >= 8 and max(bw, bh) >= 80 and bw * bh >= 1200
                    and cv2.contourArea(contour) >= 0.7 * bw * bh)
            if not (normal or thin) or bw * bh > 640 * 360 * 0.45:
                continue
            if (x <= 3 or x + bw >= 637) and bh >= 360 * 0.8:
                continue
            gray_blobs.append((x, y, bw, bh))
        side_blobs = _side_flat_blobs(frame_bgr, int(bottom_limit * 360 / h)) if self._side else []
        blobs = []
        accepted_side_blobs = []
        for index, (x, y, bw, bh) in enumerate(side_blobs + gray_blobs):
            candidate = (int(x * w / 640), int(y * h / 360),
                         min(w - 1, int((x + bw) * w / 640)), min(bottom_limit - 1, int((y + bh) * h / 360)))
            def contained(box: tuple[int, int, int, int], outer=candidate) -> bool:
                return (outer[0] <= box[0] and outer[1] <= box[1]
                        and outer[2] >= box[2] and outer[3] >= box[3])
            if any(_mostly_contained(candidate, blob) for blob in accepted_side_blobs):
                continue
            if not any((_iou(*candidate, *box) > 0.25 or _mostly_contained(candidate, box))
                       and not contained(box) for box in boxes):
                blobs.append(candidate)
                if index < len(side_blobs):
                    accepted_side_blobs.append(candidate)
                boxes = [box for box in boxes if not contained(box)]
        merge_boxes = cluster_boxes(blobs + boxes)
        diagnostic_view = None
        if len(merge_boxes) > 1 and self._cfg.merge_diagnostics and logger.isEnabledFor(logging.INFO):
            now = time.monotonic()
            if now - self._merge_diagnostic_at >= 5:
                diagnostic_view = "side" if self._side else "top"
                self._merge_diagnostic_at = now
        merge_boxes = _merge_bottom_fragments(merge_boxes, h, side=self._side, diagnostic_view=diagnostic_view)
        confirmed_unions = []
        adjacent = _merge_adjacent_boxes(
            merge_boxes, gray, threshold, diagnostic_view="side_geometry_candidate" if self._side and diagnostic_view else diagnostic_view,
            recent_unions=tuple(box for frame in self._recent_top_unions for box in frame),
            confirmed_unions=None if self._side else confirmed_unions,
        )
        framed = merge_boxes if self._side else adjacent
        if not self._side:
            # A carried merge never renews its own two-frame window.
            self._recent_top_unions = [*self._recent_top_unions[-1:], tuple(confirmed_unions)]
        reference_merge = self._cfg.side_reference_merge if self._side else self._cfg.top_reference_merge
        if (reference_merge and len(framed) > 1
                and reference_delta is not None and reference_delta.shape == (h, w)):
            framed = _merge_reference_fragments(
                framed, reference_delta, self._cfg.reference_weak_delta, self._cfg.reference_strong_delta,
                side=self._side, diagnostic_view=diagnostic_view,
            )
        elif (self._cfg.reference_side if self._side else self._cfg.reference_top) is None and len(framed) > 1:
            framed = _merge_wrappers(framed, frame_bgr[:bottom_limit], self._cfg.reference_weak_delta,
                                     diagnostic_view=diagnostic_view, side=self._side)

        # asosiasi IoU ke track parcel yang ada (greedy)
        matched: dict[int, int] = {}  # pid -> idx box
        used: set[int] = set()
        for pid, st in sorted(self._tracks.items(), key=lambda item: item[1]["misses"]):
            best_i, best_iou = None, 0.25
            for i, fb in enumerate(framed):
                if i in used:
                    continue
                iou = _iou(*fb, *st["bbox"])
                if iou > best_iou:
                    best_i, best_iou = i, iou
            if best_i is None and st["misses"] <= 8:
                for i, fb in enumerate(framed):
                    if i in used:
                        continue
                    cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
                    prev = st["bbox"]
                    distance = np.hypot(cx - (prev[0] + prev[2]) / 2, cy - (prev[1] + prev[3]) / 2)
                    if distance < 0.4 * np.hypot(fb[2] - fb[0], fb[3] - fb[1]):
                        best_i = i
                        break
            if best_i is not None:
                matched[pid] = best_i
                used.add(best_i)

        out: list[ParcelDetection] = []
        for pid, i in matched.items():
            st = self._tracks[pid]
            st["bbox"] = framed[i]
            st["misses"] = 0
            out.append(ParcelDetection(pid, framed[i], st["conf"]))
        for pid, st in self._tracks.items():
            if pid not in matched:
                st["misses"] += 1
                if st["misses"] <= 2 and not any(_iou(*st["bbox"], *box) > 0 for box in framed):
                    out.append(ParcelDetection(pid, st["bbox"], st["conf"], observed=False))
        self._tracks = {p: s for p, s in self._tracks.items() if s["misses"] <= 15}
        for i, fb in enumerate(framed):
            if i in used:
                continue
            conf = max((d.conf for d in dets), default=0.0)
            pid = self._next_pid
            self._next_pid += 1
            self._tracks[pid] = {"bbox": fb, "misses": 0, "conf": conf}
            out.append(ParcelDetection(pid, fb, conf))
        return out


def _iou(ax1: int, ay1: int, ax2: int, ay2: int, bx1: int, by1: int, bx2: int, by2: int) -> float:
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    a = (ax2 - ax1) * (ay2 - ay1)
    b = (bx2 - bx1) * (by2 - by1)
    return inter / (a + b - inter) if a + b - inter > 0 else 0.0
