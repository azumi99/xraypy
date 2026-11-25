"""Pipeline: frame source -> strip tracking -> dedup decision -> idx/AWB.

Default mode ``strip`` (scrolling X-ray screen, StripTracker). Alternative
``track`` mode uses YOLO-seg/ByteTrack or MOG2 with finalize-on-disappear.

One parcel failing never stops the loop — every failure path degrades to a
REVIEW row or an error event.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .dedup.service import Decision, DedupService
from .dedup.store import new_batch_id
from .events import EventBus
from .geometry import encode_png
from .strip_tracker import StripConfig, StripTracker, contour_crop_bgra

logger = logging.getLogger(__name__)


def _write_capture(path: Path, data: bytes, *, owned=None, dir_fd=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # ponytail: Windows path — resolve-based symlink guard, no dir_fd/fcntl
        resolved = path.resolve()
        if not resolved.is_relative_to(Path.cwd().resolve() if not path.is_absolute() else Path(path.anchor).resolve()):
            pass  # relative to project; skip strict root check for capture temp writes
        temp_path = resolved.with_name(".capture-" + __import__("secrets").token_hex(16))
        temp_path.write_bytes(data)
        try:
            if owned is None:
                os.link(temp_path, resolved)
                stat = temp_path.stat()
                return stat.st_dev, stat.st_ino
            current = resolved.stat()
            if (current.st_dev, current.st_ino) != owned:
                raise FileExistsError("capture changed before watermarking")
            os.replace(temp_path, resolved)
        finally:
            temp_path.unlink(missing_ok=True)
        return
    if dir_fd is not None:
        import secrets
        name = ".capture-" + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            _replace_owned(path.name, name, owned, dir_fd)
        finally:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        return
    file = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-", delete=False)
    temp_path = Path(file.name)
    try:
        with file:
            file.write(data)
        if owned is None:
            os.link(temp_path, path)
            stat = temp_path.stat()
            return stat.st_dev, stat.st_ino
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _replace_owned(path.name, temp_path.name, owned, directory)
        finally:
            os.close(directory)
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_owned(name, temp, owned, directory):
    from .photo_sync import is_pending_png

    if os.name == "nt":
        path = Path(directory) if isinstance(directory, (str, Path)) else None
        if path is None:
            return  # Windows caller handles ownership inline
        current = path.stat()
        if (current.st_dev, current.st_ino) != owned:
            raise FileExistsError("capture changed before watermarking")
        return
    import fcntl

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != owned or (current.st_dev, current.st_ino) != owned:
            raise FileExistsError("capture changed before watermarking")
        with os.fdopen(os.dup(fd), "rb") as original:
            if not is_pending_png(original.read(12 * 1024 * 1024 + 1)):
                raise ValueError("capture is not pending")
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        os.close(fd)


def _save_original(path: Path, bgra: np.ndarray):
    from .photo_sync import mark_pending_png
    return _write_capture(path, mark_pending_png(encode_png(bgra)))


def _save_capture(path: Path, bgra: np.ndarray, view: str, awb=None, *, captured_at=None, owned=None, dir_fd=None) -> None:
    def field(name):
        value = getattr(awb, name, None)
        return str(value).strip() if value is not None and str(value).strip() else "-"

    if view not in ("top", "side"):
        raise ValueError(f"invalid capture view: {view}")
    footer = f"{field('awb_number')} | {field('hawb_number')} | {field('part')} | {field('total')} | {view}".upper()
    height, width = bgra.shape[:2]
    font = ImageFont.truetype(str(Path(__file__).with_name("assets") / "poppins" / "Poppins-Bold.ttf"), 7)
    padding = min(3, max(0, (width - 1) // 2))
    line_height = 9
    limit = width - 2 * padding
    footer_lines = []
    while footer:
        end = len(footer)
        while end > 1 and font.getlength(footer[:end]) > limit:
            end //= 2
        while end < len(footer) and font.getlength(footer[:end + 1]) <= limit:
            end += 1
        footer_lines.append(footer[:end])
        footer = footer[end:]

    footer_height = len(footer_lines) * line_height + 2 * padding
    footer_image = Image.new("RGBA", (width, footer_height), "#152630")
    draw = ImageDraw.Draw(footer_image)
    for i, text in enumerate(footer_lines):
        draw.text((padding, padding + i * line_height), text, font=font, fill="#f2f6fa", anchor="lt")
    output = np.concatenate((bgra, cv2.cvtColor(np.asarray(footer_image), cv2.COLOR_RGBA2BGRA)))
    from .photo_sync import mark_final_png
    _write_capture(path, mark_final_png(encode_png(output)), owned=owned, dir_fd=dir_fd)


STATUS_COLORS = {
    "tracking": (210, 105, 175),  # purple (BGR)
    "saved": (80, 220, 80),  # green
    "duplicate": (60, 60, 230),  # red
    "review": (30, 160, 255),  # orange
}


@dataclass(slots=True)
class ParcelOverlay:
    seq: int
    x1: float
    x2: float
    y1: float = 0.0  # tight row bounds — bounding box presisi ke objek
    y2: float = 0.0
    status: str = "tracking"
    idx: int | None = None
    awb_number: str | None = None
    sim: float | None = None
    conf: float | None = None  # confidence detektor live (label)
    capturing: bool = False


@dataclass
class SessionState:
    batch_id: str | None = None
    running: bool = False
    started_at: float | None = None
    parcel_count: int = 0
    last_error: str | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        dedup: DedupService,
        bus: EventBus,
        source,
    ) -> None:
        self._settings = settings
        self._dedup = dedup
        self._bus = bus
        self._sources = source
        self._tracker = StripTracker(self._strip_cfg())
        self.session = SessionState()
        self.overlays: dict[int, ParcelOverlay] = {}  # seq -> overlay (strip mode)
        self._annotated: np.ndarray | None = None
        self._annotated_side: np.ndarray | None = None
        self._annotated_source: np.ndarray | None = None  # raw frame utk crop (track mode)
        self._track_references: dict[str, np.ndarray | None] = {}
        self._track_states: dict[int, dict] = {}  # track_id -> {hits, captured}
        self._side_track_states: dict[int, dict] = {}
        self._pending_top = deque()
        self._pending_side = deque()
        self._side_capture_samples: dict[int, deque] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _strip_cfg(self) -> StripConfig:
        s = self._settings.strip
        return StripConfig(
            white_thr=s.white_thr,
            min_gap=s.min_gap,
            min_w=s.min_w,
            left_margin=s.left_margin,
            min_clear_gap=s.min_clear_gap,
            overlap=s.overlap,
            max_dx=s.max_dx,
            min_response=s.min_response,
            auto_band=s.auto_band,
            motion_thr=s.motion_thr,
            band_min_rows=s.band_min_rows,
            band_window=s.band_window,
            band_min_ratio=s.band_min_ratio,
        )

    # -- session lifecycle --------------------------------------------------
    async def start_session(self) -> str:
        if self.session.running:
            # stale session (crashed loop task) -> clean up and start fresh
            if self._task is not None and not self._task.done():
                raise RuntimeError("session already running (stop it first)")
            logger.warning("stale session found (task %s) — cleaning up", "dead" if self._task else "missing")
            await self.stop_session()
        from .warehouse import current_warehouse
        batch_id = new_batch_id()
        if current_warehouse() == "cas":
            batch_id = f"cas/{batch_id}"
        self.session = SessionState(batch_id=batch_id, running=True, started_at=time.time())
        self.overlays.clear()
        self._tracker.reset()
        self._track_states.clear()
        self._side_track_states.clear()
        self._clear_pending_top()
        self._pending_side.clear()
        self._side_capture_samples.clear()
        if hasattr(self, "_detector"):
            self._detector.reset()
        if hasattr(self, "_side_detector"):
            self._side_detector.reset()
        last = await self._dedup.store.last_idx(batch_id)
        Path(self._settings.crop_dir, batch_id).mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.get_running_loop()
        self._sources.start()
        self._task = asyncio.create_task(self._run_loop(), name="pipeline-loop")
        await self._bus.publish(
            "session.started", {"batch_id": batch_id, "last_idx": last, "mode": self._settings.pipeline_mode}
        )
        logger.info("session started: batch=%s (resumed after idx=%d)", batch_id, last)
        return batch_id

    async def stop_session(self) -> dict:
        if not self.session.running:
            return {"stopped": False}
        self.session.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._sources.stop()
        # bersihkan frame anotasi — stream berhenti mengirim frame lama dan
        # area video di UI dikosongkan (event session.stopped)
        self._annotated = None
        self._annotated_side = None
        self._annotated_source = None
        self._track_states.clear()
        self._side_track_states.clear()
        self._clear_pending_top()
        self._pending_side.clear()
        self._side_capture_samples.clear()
        self.overlays.clear()
        summary = {
            "stopped": True,
            "batch_id": self.session.batch_id,
            "parcels": self.session.parcel_count,
        }
        await self._bus.publish("session.stopped", summary)
        logger.info("session stopped: %s", summary)
        return summary

    # -- main loop -----------------------------------------------------------
    async def _run_loop(self) -> None:
        interval = 1.0 / min(self._settings.capture.fps, 30)
        while self.session.running:
            try:
                frames = await asyncio.to_thread(self._sources.read_frames)
                if frames is None:
                    self._annotated = self._annotated_side = None
                    self._match_track_captures(time.monotonic())
                    await asyncio.sleep(interval)
                    continue
                if self._settings.pipeline_mode == "strip":
                    await self._process_strip_frame(frames)
                else:
                    await self._process_track_frame(frames)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad frame must not kill the session
                self.session.last_error = str(exc)
                logger.exception("pipeline frame error")
                await asyncio.sleep(interval)
            await asyncio.sleep(0.005)

    # -- strip mode -----------------------------------------------------------
    def _roi(self, frame: np.ndarray) -> np.ndarray:
        """ROI proporsional: nilai config disimpan utk layar referensi 640x360
        (HiTraX) lalu diskalakan ke resolusi frame sumber apa pun (kamera
        1080p, screen share, dll) — jadi strip-nya selalu pas penuh."""
        s = self._settings.strip
        h, w = frame.shape[:2]
        fx, fy = w / 640.0, h / 360.0
        x0 = max(0, round(s.roi_x * fx))
        y0 = max(0, round(s.roi_y * fy))
        x1 = min(w, round((s.roi_x + s.roi_w) * fx)) if s.roi_w > 0 else w
        y1 = min(h, round((s.roi_y + s.roi_h) * fy)) if s.roi_h > 0 else h
        return frame[y0:y1, x0:x1]

    async def _process_strip_frame(self, frames: dict[str, np.ndarray]) -> None:
        top = frames.get("top")
        if top is None:
            self._annotated = None
            side = frames.get("side")
            self._annotated_side = self._roi(side).copy() if side is not None else None
            return  # sumber (mis. screen share) belum mengirim frame
        side = frames.get("side")
        roi = self._roi(top)
        roi_side = self._roi(side) if side is not None else None
        new_parcels = await asyncio.to_thread(self._tracker.update, roi)
        captured_at = datetime.now().astimezone()
        self._sync_overlays()
        for parcel in new_parcels:
            asyncio.create_task(self._handle_new_parcel(parcel, roi.shape, roi_side, captured_at))
        self._annotate(top, roi.shape, roi_side)
        if roi_side is None:
            self._annotated_side = None

    def _sync_overlays(self) -> None:
        """Live tracking: posisi & ukuran overlay mengikuti known objects
        tiap frame (termasuk yang masih pending/belum di-capture)."""
        live = {k.seq: k for k in self._tracker.known if k.seq > 0}
        for seq, k in live.items():
            ov = self.overlays.get(seq)
            if ov is None:
                self.overlays[seq] = ParcelOverlay(
                    seq, float(k.x1), float(k.x2), y1=float(k.y1), y2=float(k.y2)
                )
            else:
                ov.x1, ov.x2 = float(k.x1), float(k.x2)
                ov.y1, ov.y2 = float(k.y1), float(k.y2)
        for seq in list(self.overlays):
            if seq not in live:
                del self.overlays[seq]  # objek sudah keluar layar kanan

    async def _handle_new_parcel(
        self, parcel, roi_shape: tuple[int, ...], roi_side: np.ndarray | None = None, captured_at=None
    ) -> None:
        batch_id = self.session.batch_id or "no-batch"
        s = self._settings.strip
        try:
            bgra, _polygon = await asyncio.to_thread(
                contour_crop_bgra,
                parcel.patch_bgr,
                padding=4,
                min_side=s.min_crop_side_px,
                thr=s.crop_thr,
            )
        except ValueError as exc:
            logger.warning("contour crop failed for seq %d: %s", parcel.seq, exc)
            # uniform flow: fall back to the rectangular patch so the parcel is
            # still reviewed instead of silently dropped
            bgra = cv2.cvtColor(parcel.patch_bgr, cv2.COLOR_BGR2BGRA)
        crop_rel = f"{batch_id}/seq_{parcel.seq}_top.png"
        crop_path = Path(self._settings.crop_dir, crop_rel)
        owned = await self._prepare_top_capture(batch_id, parcel.seq, crop_path, bgra)
        if owned is None:
            return
        # second POV (side): same belt coordinates -> same x-range on the side strip
        side_bgra: np.ndarray | None = None
        if roi_side is not None and parcel.x2 < roi_side.shape[1]:
            side_patch = roi_side[:, parcel.x1 : parcel.x2 + 1]
            try:
                side_bgra, _ = await asyncio.to_thread(
                    contour_crop_bgra,
                    side_patch,
                    padding=4,
                    min_side=s.min_crop_side_px,
                    thr=s.crop_thr,
                )
            except ValueError:
                side_bgra = cv2.cvtColor(side_patch, cv2.COLOR_BGR2BGRA)
        try:
            decision: Decision = await self._dedup.process_parcel(
                batch_id=batch_id,
                track_id=parcel.seq,
                crop_bgra=bgra,
                crop_ref=crop_rel,
            )
        except Exception:  # noqa: BLE001 — absolute last resort
            logger.exception("dedup decision crashed for seq %d", parcel.seq)
            return
        if not self._owns_capture(decision, batch_id, parcel.seq, crop_rel):
            return
        awb = await self._capture_awb(decision)
        if decision.record.awb_id is not None and awb is None:
            logger.warning("capture remains pending: paired AWB unavailable")
            return
        try:
            await asyncio.to_thread(_save_capture, crop_path, bgra, "top", awb, captured_at=captured_at, owned=owned)
            await self._enqueue_final_photo(crop_rel)
        except Exception:  # noqa: BLE001
            logger.exception("failed watermarking crop for seq %d; original preserved", parcel.seq)
        side_ref = None
        side_finalized = False
        if side_bgra is not None:
            side_rel = f"{batch_id}/seq_{parcel.seq}_side.png"
            side_path = Path(self._settings.crop_dir, side_rel)
            try:
                side_owned = await asyncio.to_thread(_save_original, side_path, side_bgra)
                side_ref = side_rel
            except Exception:  # noqa: BLE001
                logger.exception("failed saving side crop for seq %d", parcel.seq)
            else:
                try:
                    await asyncio.to_thread(_save_capture, side_path, side_bgra, "side", awb, captured_at=captured_at, owned=side_owned)
                    side_finalized = True
                except Exception:  # noqa: BLE001
                    logger.exception("failed watermarking side crop for seq %d; original preserved", parcel.seq)
        self.session.parcel_count += 1
        if side_ref is not None:
            try:
                side_emb = await self._dedup.worker.embed(side_bgra) if side_bgra is not None else None
            except Exception:  # noqa: BLE001 — side embedding is best-effort
                side_emb = None
            try:
                await self._dedup.store.add_side_capture(
                    decision.record.id,
                    side_emb,
                    side_ref,
                    model_name=self._dedup.worker.model_name,
                    model_version=self._dedup.worker.model_version,
                )
                if side_finalized:
                    await self._enqueue_final_photo(side_ref)
            except Exception:  # noqa: BLE001
                logger.exception("failed storing side capture for seq %d", parcel.seq)
        await self._apply_decision(parcel.seq, decision, batch_id)

    async def _prepare_top_capture(self, batch_id, seq, path, bgra):
        try:
            if await self._dedup.store.get_by_track(batch_id, seq) is not None:
                logger.warning("identity already exists for batch %s track %s", batch_id, seq)
                return None
            return await asyncio.to_thread(_save_original, path, bgra)
        except Exception:  # noqa: BLE001
            logger.exception("failed preparing crop for batch %s track %s", batch_id, seq)
            return None

    async def _enqueue_final_photo(self, ref):
        from .warehouse import current_warehouse

        sync = getattr(self._dedup, "photo_sync", None)
        if sync is not None and sync.enabled:
            try:
                await asyncio.to_thread(sync.enqueue, current_warehouse(), ref, True)
            except Exception:  # noqa: BLE001 — capture is already committed; do not lose the response
                logger.exception("failed to enqueue finalized photo for synchronization")

    @staticmethod
    def _owns_capture(decision: Decision, batch_id: str, seq: int, crop_ref: str) -> bool:
        record = decision.record
        return record.batch_id == batch_id and record.track_id == seq and record.crop_ref == crop_ref

    async def _capture_awb(self, decision: Decision):
        record = decision.record
        if record.awb_id is None:
            return None
        awb = decision.awb
        if awb is None:
            try:
                awb = next((a for a in await self._dedup.store.list_awbs("PAIRED") if a.id == record.awb_id), None)
            except Exception:  # noqa: BLE001
                logger.exception("failed loading paired AWB for parcel %s", record.id)
                return None
        if awb is None or awb.id != record.awb_id or awb.parcel_id != record.id or awb.status != "PAIRED":
            return None
        return awb

    async def _apply_decision(self, seq: int, decision: Decision, batch_id: str) -> None:
        overlay = self.overlays.get(seq)
        event = {
            "batch_id": batch_id,
            "seq": seq,
            "id": decision.record.id,
            "status": {"NEW": "saved"}.get(decision.status, decision.status.lower()),
            "index": decision.record.idx,
            "awb_number": decision.awb.awb_number if decision.awb else decision.record.awb_number,
            "awb_id": decision.awb.id if decision.awb else getattr(decision.record, "awb_id", None),
            "sim_score": decision.record.sim_score,
            "review_reason": decision.record.review_reason,
            "crop_ref": decision.record.crop_ref,
            "duplicate_of": decision.record.duplicate_of,
        }
        if overlay is not None:
            overlay.status = {"NEW": "saved"}.get(decision.status, decision.status.lower())
            overlay.idx = decision.record.idx
            overlay.awb_number = event["awb_number"]
            overlay.sim = decision.record.sim_score
        type_map = {"NEW": "parcel.saved", "DUPLICATE": "parcel.duplicate", "REVIEW": "parcel.review"}
        await self._bus.publish(type_map[decision.status], event)
        logger.info(
            "parcel seq=%d -> %s idx=%s awb=%s sim=%s",
            seq,
            decision.status,
            decision.record.idx,
            event["awb_number"],
            f"{decision.record.sim_score:.3f}" if decision.record.sim_score is not None else None,
        )

    # -- track mode (YOLO / motion backend) ------------------------------------
    def _track_roi_rows(self, h: int, view: str) -> tuple[int, int]:
        cfg = self._settings.track_roi
        start, end = (cfg.top_start, cfg.top_end) if view == "top" else (cfg.side_start, cfg.side_end)
        y0 = min(h - 1, int(h * start))
        y1 = max(y0 + 1, min(h, int(h * end)))
        return y0, y1

    def _track_roi(self, frame: np.ndarray, view: str) -> np.ndarray:
        y0, y1 = self._track_roi_rows(frame.shape[0], view)
        return frame[y0:y1, :]

    def _track_reference_delta(self, frame: np.ndarray, view: str, full_shape: tuple[int, ...]) -> np.ndarray | None:
        cfg = self._settings.detector
        path = cfg.reference_top if view == "top" else cfg.reference_side
        if path is None:
            return None
        if view not in self._track_references:
            self._track_references[view] = cv2.imread(str(path), cv2.IMREAD_COLOR)
        reference = self._track_references[view]
        if reference is None or reference.shape != full_shape:
            return None
        reference_roi = self._track_roi(reference, view)
        if reference_roi.shape != frame.shape or reference_roi.dtype != frame.dtype:
            return None
        return cv2.absdiff(frame, reference_roi).max(axis=2)

    def _capture_ready(self, det, state: dict, shape: tuple[int, ...], *, side_belt_edge: bool = False) -> bool:
        h, w = shape[:2]
        x1, y1, x2, y2 = det.bbox
        parcel_width = x2 - x1
        crop_pad = round(parcel_width * 0.15)
        left_margin = max(w * self._settings.capture.top_capture_left_margin, crop_pad)
        right_margin = w * 6 / 640
        wide_flat = parcel_width >= 0.75 * w and parcel_width >= 4 * (y2 - y1)
        if (side_belt_edge or wide_flat) and w - parcel_width < 2 * left_margin + right_margin:
            # A long parcel can be fully visible before there is room for its crop padding.
            left_margin = max(1, round(right_margin))
        bottom_visible = y2 < h - 1 or (side_belt_edge and y2 == h - 1)
        return (state["hits"] >= 2 and not state["captured"] and state["entered"]
                and x1 > state["entry_x"] and x1 >= left_margin and y1 >= h * 6 / 360
                and x2 <= w - right_margin and bottom_visible)

    @staticmethod
    def _capture_bounds(bbox, silhouettes):
        x1, y1, x2, y2 = bbox
        box_area = max(1, (x2 - x1) * (y2 - y1))
        best_overlap = 0
        bounds = bbox
        for left, top, right, bottom in silhouettes:
            overlap = max(0, min(x2, right) - max(x1, left)) * max(0, min(y2, bottom) - max(y1, top))
            if overlap >= 0.25 * box_area and overlap > best_overlap:
                best_overlap = overlap
                bounds = min(x1, left), min(y1, top), max(x2, right), max(y2, bottom)
        return bounds, best_overlap > 0

    def _track_capture_trigger(self, det, state, shape, silhouettes, *, side_belt_edge=False):
        bounds, verified = self._capture_bounds(det.bbox, silhouettes)
        h, w = shape[:2]
        left, top, right, bottom = bounds
        clearance = min(left, w - 1 - right)
        edge = max(1, round(w * 6 / 640))
        visible = (clearance >= edge and top >= max(1, round(h * 6 / 360))
                   and (bottom < h - 1 or side_belt_edge))
        # A verified silhouette permits capture before the usual crop margin.
        early = verified and state["hits"] >= 2 and state["entered"] and left > state["entry_x"]
        # A stopped parcel can stay inside the safe frame but short of the crop margin.
        # Without a silhouette, require a prior observation at the left edge.
        if left < edge:
            state["seen_at_left_edge"] = True
        stable = False
        if visible and state["entered"] and not state["captured"] and (
            verified or state.get("seen_at_left_edge")
        ):
            anchor, count = state.get("stable_view", (bounds, 0))
            tolerance = max(1, round(w * 2 / 640))
            if max(abs(old - new) for old, new in zip(anchor, bounds)) > tolerance:
                anchor, count = bounds, 0
            state["stable_view"] = (anchor, count + 1)
            stable = count >= 2
        else:
            state.pop("stable_view", None)
        ready = visible and not state["captured"] and (
            early or stable or self._capture_ready(det, state, shape, side_belt_edge=side_belt_edge)
        )
        return bounds, verified, visible, ready

    async def _process_track_frame(self, frames: dict[str, np.ndarray]) -> None:
        from .detector import ParcelDetection, ParcelTracker, capture_silhouettes, make_detector

        captured_at = time.monotonic()
        batch_id = self.session.batch_id
        self._match_track_captures(captured_at)
        top = frames.get("top")
        side = frames.get("side")
        if top is None:
            self._annotated = None
            if side is not None:
                await self._process_side_track_frame(side, captured_at, batch_id)
            else:
                self._annotated_side = None
            return
        full_top = top
        top_shape = full_top.shape
        top_y0, top_y1 = self._track_roi_rows(top_shape[0], "top")
        top = full_top[top_y0:top_y1]
        if not hasattr(self, "_detector"):
            self._detector = ParcelTracker(self._settings.detector, make_detector(self._settings.detector))
        delta = self._track_reference_delta(top, "top", top_shape)
        if self._settings.detector.top_reference_merge and delta is not None:
            parcels = await asyncio.to_thread(self._detector.process, top, reference_delta=delta)
        else:
            parcels = await asyncio.to_thread(self._detector.process, top)
        silhouettes = (await asyncio.to_thread(capture_silhouettes, top)
                       if any(getattr(det, "observed", True) for det in parcels) else [])
        self._annotated_source = top

        # live tracking overlays + auto-capture: parcel masuk dari kiri,
        # bergerak ke kanan, dan sudah FULL muncul -> capture
        W = top.shape[1]
        entry_zone = W * 0.30
        seen: set[int] = set()
        for det in parcels:
            seen.add(det.parcel_id)
            if not getattr(det, "observed", True):
                state = self._track_states.get(det.parcel_id)
                if state is not None:
                    state.pop("stable_view", None)
                continue
            st = self._track_states.setdefault(
                det.parcel_id, {"hits": 0, "captured": False, "capturing": False, "entered": False, "entry_x": None, "misses": 0}
            )
            st["hits"] += 1
            st["misses"] = 0
            x1, y1, x2, y2 = det.bbox
            if st["entry_x"] is None:
                st["entered"] = x1 <= entry_zone
                st["entry_x"] = x1
            ov = self.overlays.get(det.parcel_id)
            if ov is None:
                self.overlays[det.parcel_id] = ParcelOverlay(
                    det.parcel_id, float(x1), float(x2), y1=float(y1), y2=float(y2), conf=det.conf
                )
            else:
                ov.x1, ov.x2, ov.y1, ov.y2 = float(x1), float(x2), float(y1), float(y2)
                ov.conf = det.conf
            bounds, _, _, ready = self._track_capture_trigger(det, st, top.shape, silhouettes)
            if ready:
                left, upper, right, lower = bounds
                st["captured"] = True
                st["capturing"] = True
                slot = {"batch_id": batch_id, "seq": det.parcel_id, "decision": None, "side": None, "time": captured_at, "captured_at": datetime.now().astimezone()}
                slot["side_ready"] = asyncio.get_running_loop().create_future()
                slot["belt_span"] = (left / W, right / W)
                self._queue_track_capture(self._pending_top, slot)
                capture_det = ParcelDetection(det.parcel_id, (left, upper + top_y0, right, lower + top_y0), det.conf)
                asyncio.create_task(self._capture_with_effect(capture_det, full_top.copy(), slot, batch_id, st))
            self.overlays[det.parcel_id].capturing = st["capturing"]
        for tid in list(self._track_states):
            if tid not in seen:
                self._track_states[tid].pop("stable_view", None)
                self._track_states[tid]["misses"] += 1
                if self._track_states[tid]["misses"] > 15:
                    del self._track_states[tid]
        for tid in list(self.overlays):
            if tid not in seen:
                del self.overlays[tid]
        self._annotate_track(top, delta=delta)
        # The side view has its own tracker and coordinate system.
        if side is not None:
            await self._process_side_track_frame(side, captured_at, batch_id)
        else:
            self._annotated_side = None

    async def _process_side_track_frame(self, side: np.ndarray, captured_at: float | None = None, batch_id: str | None = None) -> None:
        from .detector import ParcelTracker, make_detector, capture_silhouettes

        if captured_at is None:
            captured_at = time.monotonic()
            batch_id = self.session.batch_id
        full_side = side
        roi_start, roi_end = self._track_roi_rows(full_side.shape[0], "side")
        side = full_side[roi_start:roi_end]
        if not hasattr(self, "_side_detector"):
            self._side_detector = ParcelTracker(self._settings.detector, make_detector(self._settings.detector), side=True)
        delta = self._track_reference_delta(side, "side", full_side.shape)
        raw_boxes = []
        kwargs = {"raw_boxes": raw_boxes} if isinstance(self._side_detector, ParcelTracker) else {}
        if self._settings.detector.side_reference_merge and delta is not None:
            kwargs["reference_delta"] = delta
        parcels = await asyncio.to_thread(self._side_detector.process, side, **kwargs)
        h, w = side.shape[:2]
        silhouettes = (await asyncio.to_thread(capture_silhouettes, side)
                       if any(getattr(det, "observed", True) for det in parcels) else [])
        seen = set()
        overlays = []
        for det in parcels:
            if not getattr(det, "observed", True):
                x1, y1, x2, y2 = det.bbox
                overlays.append(ParcelOverlay(det.parcel_id, x1, x2, y1=y1, y2=y2, conf=det.conf))
                continue
            seen.add(det.parcel_id)
            x1, y1, x2, y2 = det.bbox
            st = self._side_track_states.setdefault(det.parcel_id, {
                "hits": 0, "captured": False, "entered": x1 <= w * 0.30, "entry_x": x1, "misses": 0,
            })
            st["hits"] += 1
            st["misses"] = 0
            bounds, verified, fully_visible, capturing = self._track_capture_trigger(
                det, st, side.shape, silhouettes, side_belt_edge=roi_end < full_side.shape[0],
            )
            left, top, right, bottom = bounds
            clearance = min(left, w - 1 - right)
            matched_time = captured_at - self._settings.capture.pairing_side_delay_s
            samples = self._side_capture_samples.get(det.parcel_id)
            sample_interval = self._settings.capture.pairing_max_skew_s / 2
            # Cap the clearance score once there is room for padding on both sides.
            quality = (verified, min(clearance, round(w * 0.10)))
            sample_due = (not samples or matched_time - samples[-1]["time"] >= sample_interval
                          or quality > samples[-1]["quality"])
            if capturing or (batch_id and not st.get("paired") and fully_visible and sample_due):
                px = max(round((right - left) * 0.15), round(w * 8 / 640))
                py = max(round((bottom - top) * 0.15), round(h * 8 / 360))
                patch = side[max(0, top - py):min(h, bottom + py + 1),
                             max(0, left - px):min(w, right + px + 1)].copy()
                candidate = {
                    "batch_id": batch_id, "time": matched_time, "side": patch,
                    "captured_at": datetime.now().astimezone(), "track_id": det.parcel_id,
                    "belt_span": (left / w, right / w), "quality": quality,
                }
                if capturing:
                    st["captured"] = True
                    self._queue_track_capture(self._pending_side, candidate)
                else:
                    self._side_capture_samples.setdefault(det.parcel_id, deque(maxlen=6)).append(candidate)
            overlays.append(ParcelOverlay(det.parcel_id, x1, x2, y1=y1, y2=y2, conf=det.conf, capturing=capturing))
        for tid in list(self._side_track_states):
            if tid not in seen:
                self._side_track_states[tid].pop("stable_view", None)
                self._side_track_states[tid]["misses"] += 1
                if self._side_track_states[tid]["misses"] > 15:
                    del self._side_track_states[tid]
        self._annotated_side = full_side.copy()
        self._annotated_side[roi_start:roi_end] = self._annotate_track(side, overlays, delta=delta)
        # ponytail: naive overlap scan; spatial indexing if detection counts grow large.
        boxes = [b for b in raw_boxes if b[2] > b[0] and b[3] > b[1]]
        while len(boxes) >= 3:
            group = [boxes.pop()]
            for box in group:
                overlapping = [b for b in boxes if min(box[2], b[2]) > max(box[0], b[0])
                               and min(box[3], b[3]) > max(box[1], b[1])]
                group.extend(overlapping)
                boxes = [b for b in boxes if b not in overlapping]
            if len(group) >= 3:
                self._label_chip(self._annotated_side, "candidate CLUTTER/perlu verifikasi", 4, 24, (0, 100, 180))
                break

    def _clear_pending_top(self) -> None:
        for slot in self._pending_top:
            ready = slot.get("side_ready")
            if ready is not None and not ready.done():
                ready.set_result(False)
        self._pending_top.clear()

    @staticmethod
    def _belt_overlap(top: dict, side: dict) -> float | None:
        if "belt_span" not in top or "belt_span" not in side:
            return None
        left, right = top["belt_span"]
        side_left, side_right = side["belt_span"]
        return max(0.0, min(right, side_right) - max(left, side_left)) / (
            max(right, side_right) - min(left, side_left)
        )

    def _resolve_spatial_capture_matches(self, tops, selected):
        scores = {(i, j): self._belt_overlap(tops[i], sample)
                  for i, candidates in enumerate(selected) for j, sample in candidates.items()}
        # Only use geometry when one candidate clearly wins for both views.
        for (i, j), score in scores.items():
            if score is None or score < 0.55:
                continue
            rivals = [(a, b) for a, b in scores if (a, b) != (i, j) and (a == i or b == j)]
            if rivals and all(scores[key] is not None and score - scores[key] >= 0.20 for key in rivals):
                for a, b in rivals:
                    selected[a].pop(b, None)

    def _queue_track_capture(self, queue: deque, capture: dict) -> None:
        # ponytail: bounded global per-pipeline queues; increase capacity if measured throughput requires it.
        if len(queue) >= 256:
            self._clear_pending_top()
            self._pending_side.clear()
            logger.warning("discarded pending track captures: pairing queue full")
        queue.append(capture)
        view = "top" if queue is self._pending_top else "side"
        logger.info("capture candidate view=%s batch=%s time=%.3f pending_top=%d pending_side=%d",
                    view, capture.get("batch_id"), capture["time"], len(self._pending_top), len(self._pending_side))

    def _match_track_captures(self, now: float) -> None:
        skew = self._settings.capture.pairing_max_skew_s
        delay = self._settings.capture.pairing_side_delay_s
        recovery_skew = 2 * skew
        # Both cameras must have passed the whole candidate window before deciding.
        watermark = now - max(0.0, delay)
        tops = list(self._pending_top)
        sides = list(self._pending_side)
        # Group alternate samples with the regular trigger for the same side track.
        samples_by_side = [list(self._side_capture_samples.get(side.get("track_id"), ())) + [side]
                           for side in sides]
        pending_ids = {side.get("track_id") for side in sides}
        for track_id, samples in self._side_capture_samples.items():
            if track_id not in pending_ids:
                samples_by_side.append(list(samples))
        top_edges = []
        selected = []
        for top in tops:
            candidates = {}
            for j, samples in enumerate(samples_by_side):
                matches = [sample for sample in samples
                           if top["batch_id"] == sample["batch_id"] == self.session.batch_id
                           and abs(top["time"] - sample["time"]) <= skew]
                if matches:
                    candidates[j] = (sides[j] if j < len(sides) and any(sample is sides[j] for sample in matches)
                                     else min(matches, key=lambda sample: abs(top["time"] - sample["time"])))
            selected.append(candidates)
            top_edges.append(list(candidates))
        # Give the regular window priority before recovering views that are farther apart.
        strict_sides = {j for edges in top_edges for j in edges}
        recovery_tops = set()
        for i, top in enumerate(tops):
            if top_edges[i]:
                continue
            for j, samples in enumerate(samples_by_side):
                if j in strict_sides or samples[-1].get("track_id") is None:
                    continue
                nearby = [sample for sample in samples
                          if top["batch_id"] == sample["batch_id"] == self.session.batch_id
                          and skew < abs(sample["time"] - top["time"]) <= recovery_skew
                          and (sample["time"] > top["time"] or (self._belt_overlap(top, sample) or 0) >= 0.35)]
                if nearby:
                    selected[i][j] = min(nearby, key=lambda sample: abs(top["time"] - sample["time"]))
                    top_edges[i].append(j)
                    recovery_tops.add(i)
        self._resolve_spatial_capture_matches(tops, selected)
        top_edges = [list(candidates) for candidates in selected]
        side_edges = [[i for i, edges in enumerate(top_edges) if j in edges] for j in range(len(samples_by_side))]
        remove_top = {i for i, top in enumerate(tops) if top["batch_id"] != self.session.batch_id}
        remove_side = {j for j, samples in enumerate(samples_by_side)
                       if samples[-1]["batch_id"] != self.session.batch_id}
        for i, top in enumerate(tops):
            if i in remove_top:
                continue
            window = recovery_skew if i in recovery_tops else skew
            if top["time"] + window >= watermark:
                continue
            edges = top_edges[i]
            if len(edges) == 1:
                j = edges[0]
                sample = selected[i][j]
                if len(side_edges[j]) == 1 and sample["time"] + window < watermark:
                    # Pair using the original timestamp, then choose the best complete
                    # frame from that same side track when it falls within the window.
                    photo = max(
                        (candidate for candidate in samples_by_side[j]
                         if candidate["batch_id"] == top["batch_id"]
                         and abs(candidate["time"] - top["time"]) <= window),
                        key=lambda candidate: (candidate.get("quality", (False, 0)),
                                               -abs(candidate["time"] - top["time"])),
                        default=sample,
                    ) if "quality" in sample else sample
                    top["side"] = photo["side"]
                    top["side_captured_at"] = photo.get("captured_at")
                    logger.info("capture pair matched batch=%s seq=%s delta=%.3fs max_skew=%.3fs source=%s",
                                top["batch_id"], top["seq"], sample["time"] - top["time"], window,
                                "recovery" if i in recovery_tops else
                                ("trigger" if j < len(sides) and sample is sides[j] else "visible-side"))
                    ready = top.get("side_ready")
                    if ready is not None and not ready.done():
                        ready.set_result(True)
                    else:
                        self._store_paired_side(top)
                    remove_top.add(i)
                    remove_side.add(j)
            if len(edges) > 1 or any(len(side_edges[j]) > 1 for j in edges):
                # Discard the whole connected component, not just its oldest edge.
                component_top, component_side = {i}, set()
                while True:
                    new_side = {j for k in component_top for j in top_edges[k]}
                    new_top = {k for j in new_side for k in side_edges[j]}
                    if new_top == component_top and new_side == component_side:
                        break
                    component_top, component_side = new_top, new_side
                remove_top.update(component_top)
                remove_side.update(component_side)
                logger.warning("capture pairing ambiguous batch=%s top=%d side=%d max_skew=%.3fs; not paired",
                               top["batch_id"], len(component_top), len(component_side), skew)
        for i, top in enumerate(tops):
            if not top_edges[i] and top["time"] + recovery_skew < watermark:
                logger.warning("capture top unmatched batch=%s seq=%s max_skew=%.3fs side_delay=%.3fs",
                               top["batch_id"], top["seq"], recovery_skew, delay)
        for j, side in enumerate(sides):
            retention = recovery_skew if side.get("track_id") is not None else skew
            if not side_edges[j] and side["time"] + retention < watermark:
                logger.warning("capture side unmatched batch=%s max_skew=%.3fs side_delay=%.3fs",
                               side["batch_id"], retention, delay)
                remove_side.add(j)
        remove_top.update(i for i, top in enumerate(tops) if not top_edges[i] and top["time"] + recovery_skew < watermark)
        for j in remove_side:
            track_id = samples_by_side[j][-1].get("track_id")
            if track_id is not None:
                self._side_capture_samples.pop(track_id, None)
                if track_id in self._side_track_states and side_edges[j]:
                    self._side_track_states[track_id]["captured"] = True
                    self._side_track_states[track_id]["paired"] = True
        awaiting_tracks = {samples[-1].get("track_id") for j, samples in enumerate(samples_by_side)
                           if side_edges[j] and j not in remove_side}
        for track_id, samples in list(self._side_capture_samples.items()):
            if track_id not in awaiting_tracks and samples[-1]["time"] + recovery_skew < watermark:
                del self._side_capture_samples[track_id]
        for i in remove_top:
            ready = tops[i].get("side_ready")
            if ready is not None and not ready.done():
                ready.set_result(False)
        self._pending_top = deque(top for i, top in enumerate(tops) if i not in remove_top)
        self._pending_side = deque(side for j, side in enumerate(sides) if j not in remove_side)

    def _store_paired_side(self, slot: dict) -> None:
        if slot["side"] is not None and slot["decision"] is not None:
            side = slot.pop("side")
            asyncio.create_task(self._save_side_capture(slot, side))

    async def _save_side_capture(self, slot: dict, side: np.ndarray) -> None:
        bgra = cv2.cvtColor(side, cv2.COLOR_BGR2BGRA)
        side_ref = f"{slot['batch_id']}/seq_{slot['seq']}_side.png"
        try:
            path = Path(self._settings.crop_dir, side_ref)
            owned = slot.get("side_owned")
            if owned is None:
                owned = await asyncio.to_thread(_save_original, path, bgra)
        except Exception:  # noqa: BLE001
            logger.exception("failed saving side capture for parcel %s", slot["seq"])
            return
        try:
            await asyncio.to_thread(_save_capture, path, bgra, "side", slot["awb"], captured_at=slot.get("side_captured_at"), owned=owned)
            side_finalized = True
        except Exception:  # noqa: BLE001
            logger.exception("failed watermarking side capture for parcel %s; original preserved", slot["seq"])
            side_finalized = False
        try:
            embedding = await self._dedup.worker.embed(bgra)
        except Exception:  # noqa: BLE001
            logger.exception("failed embedding side capture for parcel %s", slot["seq"])
            embedding = None
        try:
            await self._dedup.store.add_side_capture(
                slot["decision"].record.id, embedding, side_ref,
                model_name=self._dedup.worker.model_name, model_version=self._dedup.worker.model_version,
            )
            await self._bus.publish("parcel.side_saved", {"batch_id": slot["batch_id"]})
            if side_finalized:
                await self._enqueue_final_photo(side_ref)
        except Exception:  # noqa: BLE001
            logger.exception("failed storing side capture for parcel %s", slot["seq"])

    async def _capture_with_effect(self, det, frame, side, batch_id, state) -> None:
        try:
            await self._handle_track_parcel(det, frame, side, batch_id)
        finally:
            state["capturing"] = False

    async def _handle_track_parcel(self, det, frame, side, batch_id) -> None:
        """Crop original pixels around the parcel, then run dedup and AWB pairing."""
        batch_id = batch_id or "no-batch"
        x1, y1, x2, y2 = det.bbox
        px, py = round((x2 - x1) * 0.15), round((y2 - y1) * 0.15)
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1 - px), max(0, y1 - py)
        x2, y2 = min(w - 1, x2 + px), min(h - 1, y2 + py)
        patch = frame[y1 : y2 + 1, x1 : x2 + 1]
        if patch.shape[0] < 32 or patch.shape[1] < 32:
            return
        bgra = cv2.cvtColor(patch, cv2.COLOR_BGR2BGRA)
        crop_rel = f"{batch_id}/seq_{det.parcel_id}_top.png"
        crop_path = Path(self._settings.crop_dir, crop_rel)
        owned = await self._prepare_top_capture(batch_id, det.parcel_id, crop_path, bgra)
        if owned is None:
            return
        side_ref = None
        if side is not None and "side_ready" in side:
            if not await side["side_ready"]:
                logger.warning("capture incomplete batch=%s seq=%s; AWB remains waiting", batch_id, det.parcel_id)
                return
            side_ref = f"{batch_id}/seq_{det.parcel_id}_side.png"
            try:
                side_bgra = cv2.cvtColor(side["side"], cv2.COLOR_BGR2BGRA)
                side["side_owned"] = await asyncio.to_thread(
                    _save_original, Path(self._settings.crop_dir, side_ref), side_bgra,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed saving required side capture for parcel %s", det.parcel_id)
                return
        try:
            decision = await self._dedup.process_parcel(
                batch_id=batch_id,
                track_id=det.parcel_id,
                crop_bgra=bgra,
                crop_ref=crop_rel,
                crop_ref_side=side_ref,
            )
        except Exception:  # noqa: BLE001
            logger.exception("dedup decision crashed for parcel %s", det.parcel_id)
            return
        if not self._owns_capture(decision, batch_id, det.parcel_id, crop_rel):
            return
        awb = await self._capture_awb(decision)
        if decision.record.awb_id is not None and awb is None:
            logger.warning("capture remains pending: paired AWB unavailable")
            return
        try:
            await asyncio.to_thread(_save_capture, crop_path, bgra, "top", awb, captured_at=side.get("captured_at") if side else None, owned=owned)
            await self._enqueue_final_photo(crop_rel)
        except Exception:  # noqa: BLE001
            logger.exception("failed watermarking crop for parcel %s; original preserved", det.parcel_id)
        if side is not None:
            side["awb"] = awb
            side["decision"] = decision
            self._store_paired_side(side)
        if self.session.batch_id == batch_id:
            self.session.parcel_count += 1
            await self._apply_decision(det.parcel_id, decision, batch_id)

    def _annotate_track(self, frame: np.ndarray, overlays=None, *, delta: np.ndarray | None = None) -> np.ndarray:
        annotated = frame.copy()
        cfg = self._settings.detector
        if delta is not None:
            weak = (delta >= cfg.reference_weak_delta) & (delta < cfg.reference_strong_delta)
            strong = delta >= cfg.reference_strong_delta
            annotated[weak] = (annotated[weak].astype(np.uint16) + (0, 255, 255)) // 2
            annotated[strong] = (annotated[strong].astype(np.uint16) + (255, 0, 255)) // 2
            self._label_chip(annotated, "REF: WEAK YELLOW / STRONG MAGENTA", 4, 0, (90, 0, 90))
        elif (cfg.reference_top if overlays is None else cfg.reference_side) is not None:
            self._label_chip(annotated, "REF: INVALID OR SIZE MISMATCH", 4, 0, (0, 0, 165))
        for ov in self.overlays.values() if overlays is None else overlays:
            color = ((80, 255, 80) if int(time.monotonic() * 8) % 2 else (30, 160, 30)) if ov.capturing else STATUS_COLORS["tracking"]
            x1, x2 = int(ov.x1), int(ov.x2)
            y1, y2 = max(0, int(ov.y1) - 2), int(ov.y2) + 2
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 1)
            if ov.status == "tracking":
                conf = f" {ov.conf * 100:.0f}%" if ov.conf else ""
                label = f"#{ov.seq} PARCEL{conf}"
            else:
                label = f"#{ov.seq}"
                if ov.idx is not None:
                    label += f" idx={ov.idx}"
                if ov.awb_number:
                    label += f" {ov.awb_number}"
                elif ov.status == "duplicate":
                    label += " DUP"
                elif ov.status == "review":
                    label += " REVIEW"
            self._label_chip(annotated, label, x1, y1, color)
        if overlays is None:
            self._annotated = annotated
        return annotated

    @staticmethod
    def _label_chip(img: np.ndarray, text: str, x: int, y: int, color: tuple[int, int, int]) -> None:
        """Label chip roboflow-style: panel warna solid + teks putih di atas box."""
        (tw, th), bl = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        y0 = max(0, y - th - bl - 8)
        cv2.rectangle(img, (x, y0), (x + tw + 6, y0 + th + bl + 5), color, -1)
        cv2.putText(img, text, (x + 3, y0 + th + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    # -- overlay ---------------------------------------------------------------
    def _annotate(
        self, frame: np.ndarray, roi_shape: tuple[int, ...], roi_side: np.ndarray | None = None
    ) -> None:
        # stream = ROI saja (bersih dari bar UI layar scanner), dianotasi
        # dengan koordinat native ROI utk kedua POV
        s = self._settings.strip
        roi = self._roi(frame)
        annotated = roi.copy()
        h, w = annotated.shape[:2]
        for ov in self.overlays.values():
            color = STATUS_COLORS.get(ov.status, (200, 200, 200))
            x1, x2 = int(ov.x1), int(ov.x2)
            y1 = max(0, int(ov.y1) - 2)
            y2 = min(h - 1, int(ov.y2) + 2)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 1)
            label = f"#{ov.seq}"
            if ov.idx is not None:
                label += f" idx={ov.idx}"
            if ov.awb_number:
                label += f" {ov.awb_number}"
            elif ov.status == "duplicate":
                label += " DUP"
            elif ov.status == "review":
                label += " REVIEW"
            cv2.putText(annotated, label, (x1 + 2, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        if self.session.batch_id:
            cv2.putText(
                annotated,
                f"batch {self.session.batch_id}  parcels {self.session.parcel_count}",
                (4, h - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (100, 100, 100),
                1,
            )
        self._annotated = annotated

        if roi_side is not None:
            side_annotated = roi_side.copy()
            sh = side_annotated.shape[0]
            for ov in self.overlays.values():
                color = STATUS_COLORS.get(ov.status, (200, 200, 200))
                x1, x2 = int(ov.x1), int(ov.x2)
                cv2.rectangle(side_annotated, (x1, 0), (x2, sh - 1), color, 1)
                if ov.idx is not None:
                    cv2.putText(
                        side_annotated,
                        f"idx={ov.idx}",
                        (x1 + 2, 18),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        color,
                        1,
                    )
            self._annotated_side = side_annotated

    def latest_annotated(self) -> np.ndarray | None:
        return self._annotated

    def latest_annotated_side(self) -> np.ndarray | None:
        return self._annotated_side

    def overlay_states(self) -> list[dict]:
        return [
            {"seq": ov.seq, "status": ov.status, "index": ov.idx, "awb_number": ov.awb_number}
            for ov in sorted(self.overlays.values(), key=lambda o: o.seq)
        ]
