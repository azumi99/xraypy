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
from dataclasses import dataclass

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
# 1. deteksi yang berdekatan/overlap di-kluster -> satu parcel
# 2. tiap sisi kotak di-snap ke tepi paket (garis gradien terkuat, Scharr)
# 3. tracking (IoU) + id stabil dilakukan di level parcel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParcelDetection:
    parcel_id: int
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 — kotak parcel utuh
    conf: float = 0.0
    observed: bool = True


def _boxes_overlap_or_near(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int], gap: int
) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return (
        ax1 - gap <= bx2 and bx1 - gap <= ax2 and ay1 - gap <= by2 and by1 - gap <= ay2
    )


def cluster_boxes(
    boxes: list[tuple[int, int, int, int]], gap: int
) -> list[tuple[int, int, int, int]]:
    """Union-find: kotak yang berdekatan/overlap digabung jadi satu kotak union."""
    n = len(boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _boxes_overlap_or_near(boxes[i], boxes[j], gap):
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    out = []
    for idx in clusters.values():
        xs1 = min(boxes[i][0] for i in idx)
        ys1 = min(boxes[i][1] for i in idx)
        xs2 = max(boxes[i][2] for i in idx)
        ys2 = max(boxes[i][3] for i in idx)
        out.append((xs1, ys1, xs2, ys2))
    return out


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

    def reset(self) -> None:
        self._tracks.clear()
        self._next_pid = 1
        self._backend.reset()

    def process(self, frame_bgr: np.ndarray) -> list[ParcelDetection]:
        dets = self._backend.process(frame_bgr)
        if self._side:
            dets = [d for d in dets if getattr(d, "observed", True)]
        h, w = frame_bgr.shape[:2]
        bottom_limit = max(1, int(h * self._cfg.viewport_bottom)) if self._cfg.viewport_bottom is not None else h
        gap = max(12, int(w * 0.04))
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        boxes = [(x1, y1, x2, min(y2, bottom_limit - 1))
                 for d in dets for x1, y1, x2, y2 in [d.bbox]
                 if y1 < bottom_limit and x2 > x1 and min(y2, bottom_limit - 1) > y1]
        # Reference scanner uses a bright belt: assist COCO YOLO with dark blobs.
        small = cv2.resize(gray, (640, 360))
        small = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(small)
        band = small[3:14]
        mean, std = float(band.mean()), float(band.std())
        bg = float(np.percentile(small, 90)) if mean < 140 or std > 28 else mean
        threshold = np.clip(round(bg - max(14, 12 + std * 2)), 20, 235)
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
        mask[min(310, int(bottom_limit * 360 / h)):] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = []
        for contour in contours:
            x, y, bw, bh = cv2.boundingRect(contour)
            if bw < 45 or bh < 25 or bw * bh < 3000 or bw * bh > 640 * 360 * 0.45:
                continue
            if (x <= 3 or x + bw >= 637) and bh >= 360 * 0.8:
                continue
            candidate = (int(x * w / 640), int(y * h / 360),
                         min(w - 1, int((x + bw) * w / 640)), min(bottom_limit - 1, int((y + bh) * h / 360)))
            def contained(box: tuple[int, int, int, int]) -> bool:
                return (candidate[0] <= box[0] and candidate[1] <= box[1]
                        and candidate[2] >= box[2] and candidate[3] >= box[3])
            if not any(_iou(*candidate, *box) > 0.25 and not contained(box) for box in boxes):
                blobs.append(candidate)
                boxes = [box for box in boxes if not contained(box)]
        framed = blobs + cluster_boxes(boxes, gap)

        # asosiasi IoU ke track parcel yang ada (greedy)
        matched: dict[int, int] = {}  # pid -> idx box
        used: set[int] = set()
        for pid, st in self._tracks.items():
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
                if self._side and st["misses"] <= 2 and not any(_iou(*st["bbox"], *box) > 0 for box in framed):
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
