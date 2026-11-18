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

from .config import Settings
from .dedup.service import Decision, DedupService
from .dedup.store import new_batch_id
from .events import EventBus
from .geometry import encode_png
from .strip_tracker import StripConfig, StripTracker, contour_crop_bgra

logger = logging.getLogger(__name__)


def _write_capture(path: Path, data: bytes, *, owned=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-", delete=False)
    temp_path = Path(file.name)
    try:
        with file:
            file.write(data)
        if owned is None:
            os.link(temp_path, path)
            stat = temp_path.stat()
            return stat.st_dev, stat.st_ino
        stat = path.stat()
        if (stat.st_dev, stat.st_ino) != owned:
            raise FileExistsError(f"capture changed before watermarking: {path}")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _save_original(path: Path, bgra: np.ndarray):
    return _write_capture(path, encode_png(bgra))


def _save_capture(path: Path, bgra: np.ndarray, view: str, awb=None, *, captured_at=None, owned=None) -> None:
    def field(name):
        value = getattr(awb, name, None)
        return str(value).strip() if value is not None and str(value).strip() else "-"

    if view not in ("top", "side"):
        raise ValueError(f"invalid capture view: {view}")
    footer = f"{field('awb_number')} | {field('hawb_number')} | {field('part')} | {field('total')} | {view}".upper()
    height, width = bgra.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    canvas_width = max(width, 1)
    scale, padding, line_height = 0.27, 3, 12

    def lines(text, max_width=None):
        limit = max_width if max_width is not None else canvas_width - 2 * padding
        result = []
        while text:
            end = len(text)
            while end > 1 and cv2.getTextSize(text[:end], font, scale, 1)[0][0] > limit:
                end //= 2
            # Extend to use the available width after the initial search.
            while end < len(text) and cv2.getTextSize(text[:end + 1], font, scale, 1)[0][0] <= limit:
                end += 1
            result.append(text[:end])
            text = text[end:]
        return result

    footer_lines = lines(footer)
    footer_height = len(footer_lines) * line_height + 2 * padding
    output = np.zeros((height + footer_height, canvas_width, 4), dtype=np.uint8)
    output[height:, :, 3] = 255
    output[:height, :width] = bgra
    for i, text in enumerate(footer_lines):
        cv2.putText(output, text, (padding, height + padding + 8 + i * line_height), font, scale, (255, 255, 255, 255), 1, cv2.LINE_AA)
    from .photo_sync import mark_final_png
    _write_capture(path, mark_final_png(encode_png(output)), owned=owned)


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
        self._track_states: dict[int, dict] = {}  # track_id -> {hits, captured}
        self._side_track_states: dict[int, dict] = {}
        self._pending_top = deque()
        self._pending_side = deque()
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
        self._pending_top.clear()
        self._pending_side.clear()
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
        self._pending_top.clear()
        self._pending_side.clear()
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
    async def _process_track_frame(self, frames: dict[str, np.ndarray]) -> None:
        from .detector import ParcelTracker, make_detector

        captured_at = time.monotonic()
        batch_id = self.session.batch_id
        self._match_track_captures(captured_at)
        top = frames.get("top")
        side = frames.get("side")
        if top is None:
            self._annotated = None
            if side is not None:
                await self._process_side_track_frame(side, captured_at, batch_id)
            return
        if not hasattr(self, "_detector"):
            self._detector = ParcelTracker(self._settings.detector, make_detector(self._settings.detector))
        parcels = await asyncio.to_thread(self._detector.process, top)
        self._annotated_source = top

        # live tracking overlays + auto-capture: parcel masuk dari kiri,
        # bergerak ke kanan, dan sudah FULL muncul -> capture
        min_hits = 2
        W = top.shape[1]
        H = top.shape[0]
        entry_zone = W * 0.30
        seen: set[int] = set()
        for det in parcels:
            seen.add(det.parcel_id)
            st = self._track_states.setdefault(
                det.parcel_id, {"hits": 0, "captured": False, "capturing": False, "entered": False, "cx0": None, "misses": 0}
            )
            st["hits"] += 1
            st["misses"] = 0
            x1, y1, x2, y2 = det.bbox
            cx = (x1 + x2) / 2.0
            if st["cx0"] is None:
                st["entered"] = x1 <= entry_zone
                st["cx0"] = cx
            ov = self.overlays.get(det.parcel_id)
            if ov is None:
                self.overlays[det.parcel_id] = ParcelOverlay(
                    det.parcel_id, float(x1), float(x2), y1=float(y1), y2=float(y2), conf=det.conf
                )
            else:
                ov.x1, ov.x2, ov.y1, ov.y2 = float(x1), float(x2), float(y1), float(y2)
                ov.conf = det.conf
            # full muncul: kiri sudah jelas dari tepi + objek bergerak ke kanan
            # sejak pertama teramati + track stabil
            fully_out = (x1 >= W * 32 / 640 and y1 >= H * 6 / 360
                         and x2 <= W - W * 6 / 640 and y2 <= H * 0.86 + 1)
            if st["hits"] >= min_hits and not st["captured"] and st["entered"] and fully_out:
                st["captured"] = True
                st["capturing"] = True
                slot = {"batch_id": batch_id, "seq": det.parcel_id, "decision": None, "side": None, "time": captured_at, "captured_at": datetime.now().astimezone()}
                self._queue_track_capture(self._pending_top, slot)
                asyncio.create_task(self._capture_with_effect(det, top.copy(), slot, batch_id, st))
            self.overlays[det.parcel_id].capturing = st["capturing"]
        for tid in list(self._track_states):
            if tid not in seen:
                self._track_states[tid]["misses"] += 1
                if self._track_states[tid]["misses"] > 15:
                    del self._track_states[tid]
        for tid in list(self.overlays):
            if tid not in seen:
                del self.overlays[tid]
        self._annotate_track(top)
        # The side view has its own tracker and coordinate system.
        if side is not None:
            await self._process_side_track_frame(side, captured_at, batch_id)
        else:
            self._annotated_side = None

    async def _process_side_track_frame(self, side: np.ndarray, captured_at: float | None = None, batch_id: str | None = None) -> None:
        from .detector import ParcelTracker, make_detector

        if captured_at is None:
            captured_at = time.monotonic()
            batch_id = self.session.batch_id
        if not hasattr(self, "_side_detector"):
            self._side_detector = ParcelTracker(self._settings.detector, make_detector(self._settings.detector), side=True)
        parcels = await asyncio.to_thread(self._side_detector.process, side)
        h, w = side.shape[:2]
        seen = set()
        overlays = []
        for det in parcels:
            if not getattr(det, "observed", True):
                x1, y1, x2, y2 = det.bbox
                overlays.append(ParcelOverlay(det.parcel_id, x1, x2, y1=y1, y2=y2, conf=det.conf))
                continue
            seen.add(det.parcel_id)
            st = self._side_track_states.setdefault(det.parcel_id, {"hits": 0, "captured": False, "entered": None, "misses": 0})
            st["hits"] += 1
            st["misses"] = 0
            x1, y1, x2, y2 = det.bbox
            if st["entered"] is None:
                st["entered"] = x1 <= w * 0.30
            fully_out = (x1 >= w * 32 / 640 and y1 >= h * 6 / 360
                         and x2 <= w - w * 6 / 640 and y2 <= h * 0.86 + 1)
            capturing = st["hits"] >= 2 and not st["captured"] and st["entered"] and fully_out
            if capturing:
                st["captured"] = True
                px, py = round((x2 - x1) * 0.15), round((y2 - y1) * 0.15)
                patch = side[max(0, y1 - py):min(h, y2 + py + 1), max(0, x1 - px):min(w, x2 + px + 1)].copy()
                self._queue_track_capture(self._pending_side, {
                    "batch_id": batch_id, "time": captured_at - self._settings.capture.pairing_side_delay_s,
                    "side": patch, "captured_at": datetime.now().astimezone(),
                })
            overlays.append(ParcelOverlay(det.parcel_id, x1, x2, y1=y1, y2=y2, conf=det.conf, capturing=capturing))
        for tid in list(self._side_track_states):
            if tid not in seen:
                self._side_track_states[tid]["misses"] += 1
                if self._side_track_states[tid]["misses"] > 15:
                    del self._side_track_states[tid]
        self._annotated_side = self._annotate_track(side, overlays)

    def _queue_track_capture(self, queue: deque, capture: dict) -> None:
        # ponytail: bounded global per-pipeline queues; increase capacity if measured throughput requires it.
        if len(queue) >= 256:
            self._pending_top.clear()
            self._pending_side.clear()
            logger.warning("discarded pending track captures: pairing queue full")
        queue.append(capture)
        view = "top" if queue is self._pending_top else "side"
        logger.info("capture candidate view=%s batch=%s time=%.3f pending_top=%d pending_side=%d",
                    view, capture.get("batch_id"), capture["time"], len(self._pending_top), len(self._pending_side))

    def _match_track_captures(self, now: float) -> None:
        skew = self._settings.capture.pairing_max_skew_s
        delay = self._settings.capture.pairing_side_delay_s
        # Both cameras must have passed the whole candidate window before deciding.
        watermark = now - max(0.0, delay)
        tops = list(self._pending_top)
        sides = list(self._pending_side)
        top_edges = [[j for j, side in enumerate(sides)
                      if top["batch_id"] == side["batch_id"] == self.session.batch_id
                      and abs(top["time"] - side["time"]) <= skew] for top in tops]
        side_edges = [[i for i, edges in enumerate(top_edges) if j in edges] for j in range(len(sides))]
        remove_top, remove_side = set(), set()
        for i, top in enumerate(tops):
            if top["time"] + skew >= watermark:
                continue
            edges = top_edges[i]
            if len(edges) == 1:
                j = edges[0]
                if len(side_edges[j]) == 1 and sides[j]["time"] + skew < watermark:
                    top["side"] = sides[j]["side"]
                    top["side_captured_at"] = sides[j].get("captured_at")
                    logger.info("capture pair matched batch=%s seq=%s delta=%.3fs max_skew=%.3fs",
                                top["batch_id"], top["seq"], sides[j]["time"] - top["time"], skew)
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
            if not top_edges[i] and top["time"] + skew < watermark:
                logger.warning("capture top unmatched batch=%s seq=%s max_skew=%.3fs side_delay=%.3fs",
                               top["batch_id"], top["seq"], skew, delay)
        for j, side in enumerate(sides):
            if not side_edges[j] and side["time"] + skew < watermark:
                logger.warning("capture side unmatched batch=%s max_skew=%.3fs side_delay=%.3fs",
                               side["batch_id"], skew, delay)
        remove_top.update(i for i, top in enumerate(tops) if not top_edges[i] and top["time"] + skew < watermark)
        remove_side.update(j for j, side in enumerate(sides) if not side_edges[j] and side["time"] + skew < watermark)
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
        """Crop BERSIH dari kotak parcel (latar dibuang via kontur Otsu) ->
        dedup + AWB."""
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
        try:
            decision = await self._dedup.process_parcel(
                batch_id=batch_id,
                track_id=det.parcel_id,
                crop_bgra=bgra,
                crop_ref=crop_rel,
            )
        except Exception:  # noqa: BLE001
            logger.exception("dedup decision crashed for parcel %s", det.parcel_id)
            return
        if not self._owns_capture(decision, batch_id, det.parcel_id, crop_rel):
            return
        awb = await self._capture_awb(decision)
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

    def _annotate_track(self, frame: np.ndarray, overlays=None) -> np.ndarray:
        annotated = frame.copy()
        for ov in self.overlays.values() if overlays is None else overlays:
            color = ((80, 255, 80) if int(time.monotonic() * 8) % 2 else (30, 160, 30)) if ov.capturing else STATUS_COLORS["tracking"]
            x1, x2 = int(ov.x1), int(ov.x2)
            y1, y2 = max(0, int(ov.y1) - 2), int(ov.y2) + 2
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
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
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
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
                cv2.rectangle(side_annotated, (x1, 0), (x2, sh - 1), color, 2)
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
