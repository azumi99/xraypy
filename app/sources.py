"""Per-view source manager: each POV (tampak atas / tampak samping) has its
own configurable source:

- ``synthetic``   : simulasi layar HiTraX (demo; pair-aware — belt state
                    dibagi antar POV selama keduanya synthetic)
- ``screen``      : frame didorong browser via getDisplayMedia -> /ws/push/{view}
- ``camera:<idx>``: cv2.VideoCapture (HDMI capture card di lapangan)

Kinds diubah saat runtime lewat POST /sources; pergantian source dilakukan
tanpa menghentikan session (hot-swap per view).
"""

from __future__ import annotations

import logging
import threading
import time

import cv2

from .capture import Cv2FrameSource, ScreenShareSource, SyntheticFrameSource
from .config import CaptureConfig

logger = logging.getLogger(__name__)

VIEWS = ("top", "side")


def parse_kind(kind: str) -> tuple[str, str | None]:
    """'camera:1' -> ('camera', '1'); 'synthetic' -> ('synthetic', None)."""
    if kind.startswith("camera:"):
        return "camera", kind.split(":", 1)[1]
    if kind in ("synthetic", "screen"):
        return kind, None
    raise ValueError(f"unknown source kind: {kind!r} (use synthetic | screen | camera:<idx>)")


class ScreenFrameSource(ScreenShareSource):
    """Alias kept for readability in source manager."""


class SourceManager:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._running = False
        self._kinds: dict[str, str] = {
            "top": settings.capture.top_source,
            "side": settings.capture.side_source,
        }
        self._synth = SyntheticFrameSource(settings.capture)
        self._screen = {v: ScreenShareSource() for v in VIEWS}
        self._cams: dict[str, Cv2FrameSource] = {}
        self._camera_cache: tuple[float, list[str]] | None = None
        self._synth.start()  # preview jalan sejak awal, tanpa perlu session

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._running = True
            self._synth.start()
            for view, src in self._cams.items():
                src.start()
            logger.info("sources started: %s", self._kinds)

    def stop(self) -> None:
        # Preview TETAP jalan setelah session berhenti — yang berhenti hanya
        # proses capture paket di pipeline. Source tidak dimatikan/dibuang,
        # supaya Start Session berikutnya langsung jalan lagi.
        with self._lock:
            self._running = False
            logger.info("sources preview tetap jalan (session stop)")

    def close(self) -> None:
        """Release preview workers and cameras when the application shuts down."""
        with self._lock:
            for view in list(self._cams):
                self._release_camera(view)
            self._synth.stop()
            for source in self._screen.values():
                source.stop()

    def _ensure_camera(self, view: str, index: str) -> Cv2FrameSource:
        if view not in self._cams:
            cfg = CaptureConfig(source_kind="device", source=index)
            src = Cv2FrameSource(cfg)
            src.start()  # selalu start: preview tanpa perlu session
            self._cams[view] = src
        return self._cams[view]

    def _release_camera(self, view: str) -> None:
        src = self._cams.pop(view, None)
        if src is not None:
            src.stop()

    # -- kind switching --------------------------------------------------------
    def set_kind(self, view: str, kind: str) -> None:
        if view not in VIEWS:
            raise ValueError(f"view must be top|side, got {view!r}")
        parsed, arg = parse_kind(kind)
        with self._lock:
            self._release_camera(view)
            self._kinds[view] = parsed if arg is None else f"camera:{arg}"
            if parsed == "camera":
                self._ensure_camera(view, arg)
            logger.info("source[%s] -> %s", view, self._kinds[view])

    def push_screen(self, view: str, data: bytes) -> None:
        self._screen[view].push(data)

    # -- frames ----------------------------------------------------------------
    def read_frames(self) -> dict[str, "object"] | None:
        """Latest frame per view (dict of numpy arrays), or None bila kosong."""
        with self._lock:
            kinds = dict(self._kinds)
            screen = dict(self._screen)
            cams = dict(self._cams)
        frames: dict = {}
        if "synthetic" in kinds.values():
            synth = self._synth.read_all() or {}
            for v in VIEWS:
                if kinds[v] == "synthetic" and v in synth:
                    frames[v] = synth[v]
        for v in VIEWS:
            kind = kinds[v]
            if kind == "screen":
                f = screen[v].read()
                if f is not None:
                    frames[v] = f
            elif kind.startswith("camera:") and v in cams:
                f = cams[v].read()
                if f is not None:
                    frames[v] = f
        return frames or None

    # -- status & probing --------------------------------------------------------
    def status(self, *, probe_cameras: bool = True) -> dict:
        return {"top": self._kinds["top"], "side": self._kinds["side"], "cameras": self.list_cameras() if probe_cameras else []}

    def list_cameras(self, max_index: int = 4) -> list[str]:
        """Probe cv2 camera indices (blocking, cached 60s) — HDMI capture
        cards muncul di sini sebagai camera:0, camera:1, dst.
        Panggil via asyncio.to_thread dari router agar tidak blokir event loop."""
        now = time.monotonic()
        if self._camera_cache and now - self._camera_cache[0] < 60:
            return self._camera_cache[1]
        found = []
        for i in range(max_index):
            cap = cv2.VideoCapture(i)
            ok = cap.isOpened() and cap.read()[0]
            cap.release()
            if ok:
                found.append(f"camera:{i}")
        self._camera_cache = (now, found)
        return found
