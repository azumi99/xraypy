"""Frame sources: HDMI capture device, video file, or synthetic conveyor.

A background reader thread keeps only the latest frame (drop-old policy) so
slow consumers never build latency.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np

from .config import CaptureConfig

logger = logging.getLogger(__name__)


class FrameSource(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def read(self) -> np.ndarray | None:
        """Latest primary (top-view) BGR frame, or None when not available yet."""

    def read_all(self) -> dict[str, np.ndarray] | None:
        """Latest frames per view, e.g. {"top": ..., "side": ...}.

        Sources with a single view return {"top": frame}. None = not ready.
        """
        frame = self.read()
        return None if frame is None else {"top": frame}


class Cv2FrameSource(FrameSource):
    """Threaded cv2.VideoCapture for device indexes / files / RTSP."""

    def __init__(self, cfg: CaptureConfig) -> None:
        self._cfg = cfg
        self._cap: cv2.VideoCapture | None = None
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        src: int | str
        try:
            src = int(self._cfg.source)
        except ValueError:
            src = self._cfg.source
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            raise RuntimeError(
                f"cannot open capture source {self._cfg.source!r} — "
                "check the HDMI capture device index / file path"
            )
        if self._cfg.source_kind == "device":
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._cfg.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cfg.height)
            cap.set(cv2.CAP_PROP_FPS, self._cfg.fps)
        self._cap = cap
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capture-reader", daemon=True)
        self._thread.start()
        logger.info("capture started: %s (%s)", self._cfg.source, self._cfg.source_kind)

    def _run(self) -> None:
        assert self._cap is not None
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                if self._cfg.source_kind == "file":
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop files (dev)
                    continue
                time.sleep(0.05)
                continue
            with self._lock:
                self._frame = frame

    def read(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        logger.info("capture stopped")


class SyntheticFrameSource(FrameSource):
    """Simulates the real X-ray screen: 640x360 HiTraX-like strip.

    - light gray background (~222) like the real machine,
    - parcels look like X-ray material: organic content (orange) + metallic
      items (blue) inside a bag with a darker outline,
    - movement is SLOW; parcels accumulate toward the right and are only
      removed once fully pushed past the right edge by newer parcels
      entering from the LEFT (like the real screen after scanning),
    - dark UI bar at the bottom (must be excluded by the ROI).

    Repeated parcels (every N-th spawn) are pixel-identical -> exercise the
    dedup DUPLICATE path end-to-end.
    """

    SCREEN_W, SCREEN_H = 640, 360
    BAR_Y = 312  # bottom UI bar like the real screen
    SPEED = 0.9  # px/frame to the right — slow conveyor
    ENTER_X_OFFSET = 12  # new parcel starts fully off-screen left -> visible gap ~12px
    PAUSE_CHANCE = 0.8  # chance of pausing right after a new parcel spawns
    PAUSE_MIN_FRAMES = 60  # ~2s at 30fps
    PAUSE_MAX_FRAMES = 120  # ~4s at 30fps

    def __init__(self, cfg: CaptureConfig, duplicate_every: int = 4, views: tuple = ("top", "side"), ghost: bool = False) -> None:
        self._cfg = cfg
        self._views = tuple(views)
        self._ghost = ghost  # objek samar transparan oranye
        self._frames: dict[str, np.ndarray] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._objects: list[dict] = []  # oldest (leftmost spawned) first
        self._spawned = 0
        self._duplicate_every = duplicate_every
        self._patches: dict[tuple, np.ndarray] = {}  # (view, seed) -> pre-rendered parcel
        self._pause_frames = 0
        self._rng = np.random.default_rng()

    def start(self) -> None:
        self._stop.clear()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="synthetic-capture", daemon=True)
        self._thread.start()

    # -- parcel appearance ----------------------------------------------------
    SHAPES = ("rect", "oval", "capsule", "round")

    def _base_shape(self, shape: str, w: int, h: int, pad: int) -> np.ndarray:
        """Bentuk dasar parcel: kotak membulat / lonjong oval / kapsul / bulat."""
        m = np.zeros((h, w), np.uint8)
        cx, cy = w // 2, h // 2
        if shape == "oval":
            cv2.ellipse(m, (cx, cy), (w // 2 - pad, h // 2 - pad), 0, 0, 360, 255, -1)
        elif shape == "capsule":
            # oval pipih + band tengah = memanjang dengan ujung membulat
            cv2.ellipse(m, (cx, cy), (w // 2 - pad, h // 2 - pad), 0, 0, 360, 255, -1)
            cv2.rectangle(m, (pad, cy - (h // 2 - pad) // 2), (w - pad, cy + (h // 2 - pad) // 2), 255, -1)
        elif shape == "round":
            r = min(w, h) // 2 - pad
            cv2.ellipse(m, (cx, cy), (r, r), 0, 0, 360, 255, -1)
        else:  # rect — kotak membulat (dasar lama)
            cv2.rectangle(m, (pad, pad), (w - pad, h - pad), 255, -1)
        return m

    def _blob_mask(self, w: int, h: int, rng: np.random.Generator, shape: str = "rect") -> np.ndarray:
        """Organic blob outline (soft, bulging) — bukan kotak kaku, seperti
        tas berisi di citra X-ray. Alpha lembut di tepi (blur)."""
        m = self._base_shape(shape, w, h, int(min(w, h) * 0.05))
        # bulge/dent acak: ellipses di-gabung/dikurangkan dari bentuk dasar
        for _ in range(int(rng.integers(4, 9))):
            cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
            ax = int(rng.integers(w // 5, w // 2))
            ay = int(rng.integers(h // 5, h // 2))
            blob = np.zeros((h, w), np.uint8)
            cv2.ellipse(blob, (cx, cy), (ax, ay), int(rng.integers(0, 180)), 0, 360, 255, -1)
            blob = cv2.GaussianBlur(blob, (0, 0), min(w, h) * 0.05 + 2)
            if rng.random() < 0.65:
                m = cv2.bitwise_or(m, blob)
            else:
                # dent hanya mengikis PITA TEPI (bukan tengah parcel)
                band = cv2.subtract(m, cv2.erode(m, np.ones((17, 17), np.uint8)))
                m = cv2.bitwise_and(m, cv2.bitwise_not(cv2.bitwise_and(blob, band)))
        # tepi lembut khas X-ray
        soft = cv2.GaussianBlur(m, (0, 0), min(w, h) * 0.03 + 2)
        return soft

    def _parcel_patch(self, w: int, h: int, seed: int) -> np.ndarray:
        """Pre-render satu parcel X-ray bentuk organik (BGRA: alpha = blob)."""
        key = ("top", seed)
        if key in self._patches:
            return self._patches[key]
        rng = np.random.default_rng(seed)
        shape = self.SHAPES[seed % len(self.SHAPES)]
        alpha = self._blob_mask(w, h, rng, shape)
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        # massa organik oranye di dalam tas
        rgb[:, :, 0] = rng.integers(20, 60, (h, w), dtype=np.uint8)
        rgb[:, :, 1] = rng.integers(70, 140, (h, w), dtype=np.uint8)
        rgb[:, :, 2] = rng.integers(150, 230, (h, w), dtype=np.uint8)
        # isi metalik biru — hanya di dalam blob
        solid = (alpha > 128).astype(np.uint8)
        for _ in range(int(rng.integers(2, 6))):
            iw = int(rng.integers(max(8, w // 5), max(12, w // 2)))
            ih = int(rng.integers(max(8, h // 5), max(12, h // 2)))
            ix = int(rng.integers(0, max(1, w - iw)))
            iy = int(rng.integers(0, max(1, h - ih)))
            color = (int(rng.integers(180, 255)), int(rng.integers(90, 160)), int(rng.integers(30, 70)))
            item = np.zeros((h, w), np.uint8)
            if rng.random() < 0.5:
                cv2.rectangle(item, (ix, iy), (ix + iw, iy + ih), 255, -1)
            else:
                cv2.ellipse(item, (ix + iw // 2, iy + ih // 2), (iw // 2, ih // 2), 0, 0, 360, 255, -1)
            item = cv2.bitwise_and(item, solid)
            rgb[item > 0] = color
        # bercak gelap organik (pakaian tebal / kabel)
        for _ in range(int(rng.integers(1, 4))):
            ix, iy = int(rng.integers(0, max(1, w - 20))), int(rng.integers(0, max(1, h - 20)))
            blob = np.zeros((h, w), np.uint8)
            cv2.ellipse(blob, (ix + 10, iy + 10),
                        (int(rng.integers(8, w // 4)), int(rng.integers(6, h // 4))),
                        int(rng.integers(0, 180)), 0, 360, 255, -1)
            blob = cv2.bitwise_and(blob, solid)
            rgb[blob > 0] = (10, 40, 90)
        # blur isi supaya terlihat seperti citra X-ray
        rgb = cv2.GaussianBlur(rgb, (5, 5), 0)
        alpha = self._apply_ghost(rgb, alpha, rng)
        out = np.dstack([rgb, alpha])
        self._patches[key] = out
        return out

    def _parcel_patch_side(self, w: int, h: int, seed: int) -> np.ndarray:
        """Tampak samping: siluet teal blob + slab padat, jelas beda citra."""
        key = ("side", seed)
        if key in self._patches:
            return self._patches[key]
        rng = np.random.default_rng(seed + 777)
        shape = self.SHAPES[seed % len(self.SHAPES)]  # sama dengan tampak atas
        alpha = self._blob_mask(w, h, rng, shape)
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[:, :, 0] = rng.integers(140, 200, (h, w), dtype=np.uint8)  # teal
        rgb[:, :, 1] = rng.integers(120, 170, (h, w), dtype=np.uint8)
        rgb[:, :, 2] = rng.integers(40, 90, (h, w), dtype=np.uint8)
        solid = (alpha > 128).astype(np.uint8)
        for _ in range(int(rng.integers(2, 5))):
            iw = int(rng.integers(max(6, w // 8), max(10, w // 3)))
            ix = int(rng.integers(0, max(1, w - iw)))
            color = (int(rng.integers(30, 70)), int(rng.integers(40, 80)), int(rng.integers(60, 110)))
            item = np.zeros((h, w), np.uint8)
            cv2.rectangle(item, (ix, int(h * 0.2)), (ix + iw, int(h * 0.9)), 255, -1)
            item = cv2.bitwise_and(item, solid)
            rgb[item > 0] = color
        cv2.rectangle(rgb, (0, int(h * 0.86)), (w - 1, h - 1), (20, 25, 35), -1)
        rgb = cv2.GaussianBlur(rgb, (5, 5), 0)
        alpha = self._apply_ghost(rgb, alpha, rng)
        out = np.dstack([rgb, alpha])
        self._patches[key] = out
        return out

    def _apply_ghost(self, rgb: np.ndarray, alpha: np.ndarray, rng) -> np.ndarray:
        """Mode samar: parcel jadi transparan (alpha ~40%) dan rata oranye."""
        if not self._ghost:
            return alpha
        orange = np.zeros_like(rgb)
        orange[:, :, 0] = 60   # B
        orange[:, :, 1] = 130  # G
        orange[:, :, 2] = 235  # R
        rgb[:] = (rgb.astype(np.float32) * 0.25 + orange.astype(np.float32) * 0.75).astype(np.uint8)
        return (alpha.astype(np.float32) * 0.25).astype(np.uint8)

    def _spawn(self, x: float) -> dict:
        self._spawned += 1
        if self._spawned % self._duplicate_every == 0 and self._spawned > 2 and self._objects:
            # repeat an earlier appearance (identical pixels) -> duplicate
            src = self._objects[(self._spawned // self._duplicate_every - 1) % len(self._objects)]
            w, h, seed = src["w"], src["h"], src["seed"]
        else:
            rng = np.random.default_rng(self._spawned)
            w = int(rng.integers(90, 170))
            h = int(rng.integers(90, 170))
            seed = int(rng.integers(0, 2**31))
        self._parcel_patch(w, h, seed)  # pre-render both POVs once
        self._parcel_patch_side(w, h, seed)
        return {"x": x, "w": w, "h": h, "seed": seed}

    def _spawn_offscreen(self) -> dict:
        obj = self._spawn(0.0)
        obj["x"] = float(-obj["w"] - self.ENTER_X_OFFSET)  # fully off-screen left
        return obj

    def _render(self) -> np.ndarray:
        return self._render_pair()["top"]

    def _render_pair(self) -> dict[str, np.ndarray]:
        w, h = self.SCREEN_W, self.SCREEN_H
        frame = np.full((h, w, 3), 222, dtype=np.uint8)  # HiTraX-like light bg
        side = np.full((h, w, 3), 218, dtype=np.uint8)  # side-view screen bg

        # spawn a new parcel when the previous one's LEFT edge passes x=0 —
        # since the newcomer waits fully off-screen (ENTER_X_OFFSET), the
        # visible gap between parcels stays ~ENTER_X_OFFSET px (tight packing,
        # matching the real screen's ~10-12px gaps)
        if not self._objects or self._objects[-1]["x"] > 0:
            self._objects.append(self._spawn_offscreen())
            if self._rng.random() < self.PAUSE_CHANCE:
                # occasional stop-and-go: rest a moment after adding a parcel
                self._pause_frames = int(self._rng.integers(self.PAUSE_MIN_FRAMES, self.PAUSE_MAX_FRAMES))
        # conveyor pauses: parcels freeze (new spawns wait too), then resume
        if self._pause_frames > 0:
            self._pause_frames -= 1
        else:
            for obj in self._objects:
                obj["x"] += self.SPEED
        # parcels are only removed once FULLY pushed past the right edge —
        # they accumulate on screen like the real scanner display
        self._objects = [o for o in self._objects if o["x"] < w + 10]

        strip_h = self.BAR_Y
        for obj in self._objects:
            x = int(obj["x"])
            x0, x1 = max(0, x), min(w, x + obj["w"])
            if x1 <= x0:
                continue
            y0, y1 = (strip_h - obj["h"]) // 2, (strip_h + obj["h"]) // 2  # centered in strip
            px0 = x0 - x  # visible portion of the patch
            # compositing alpha blob: tepi organik lembut khas citra X-ray
            for target, make in ((frame, self._parcel_patch), (side, self._parcel_patch_side)):
                patch = make(obj["w"], obj["h"], obj["seed"])
                part = patch[:, px0 : px0 + (x1 - x0)]
                rgb, a = part[:, :, :3], part[:, :, 3:4].astype(np.float32) / 255.0
                region = target[y0:y1, x0:x1].astype(np.float32)
                target[y0:y1, x0:x1] = (rgb * a + region * (1.0 - a)).astype(np.uint8)

        # bottom UI bars (excluded by ROI)
        frame[self.BAR_Y :] = (60, 60, 60)
        side[self.BAR_Y :] = (60, 60, 60)
        cv2.putText(frame, "HiTraX TOP", (8, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(side, "HiTraX SIDE", (8, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        return {"top": frame, "side": side}

    def _run(self) -> None:
        interval = 1.0 / max(self._cfg.fps, 1)
        while not self._stop.is_set():
            frames = self._render_pair()
            with self._lock:
                self._frames = frames
            time.sleep(interval)

    def read(self) -> np.ndarray | None:
        with self._lock:
            if self._frames is None:
                return None
            return self._frames["top"].copy()

    def read_all(self) -> dict[str, np.ndarray] | None:
        with self._lock:
            if self._frames is None:
                return None
            return {v: f.copy() for v, f in self._frames.items() if v in self._views}

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class ScreenShareSource(FrameSource):
    """Single-view source fed by the browser via getDisplayMedia — the page
    pushes JPEG frames over /ws/push/{view} and we serve the latest one."""

    STALE_AFTER_S = 1.0

    def __init__(self) -> None:
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._received_at = 0.0

    def start(self) -> None:  # nothing to start; frames arrive via push()
        pass

    def stop(self) -> None:
        with self._lock:
            self._frame = None

    def push(self, data: bytes) -> None:
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is not None:
            with self._lock:
                self._frame = frame
                self._received_at = time.monotonic()

    def read(self) -> np.ndarray | None:
        with self._lock:
            if self._frame is not None and time.monotonic() - self._received_at >= self.STALE_AFTER_S:
                self._frame = None
            return None if self._frame is None else self._frame.copy()


def make_frame_source(cfg: CaptureConfig) -> FrameSource:
    if cfg.source_kind == "synthetic":
        return SyntheticFrameSource(cfg)
    return Cv2FrameSource(cfg)
