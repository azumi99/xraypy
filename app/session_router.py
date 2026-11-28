"""Session control + service status endpoints."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any, Literal

import cv2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .security import require_operator
from .live import live_resources, site_control_lock

router = APIRouter(tags=["session"])


@router.post("/session/start")
async def start_session(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    async with site_control_lock(request.app):
        pipeline = live_resources(request.app).pipeline
        try:
            if getattr(request.app.state.settings, "pipeline_mode", "strip") == "track":
                batch_id = await pipeline.start_session(select_awb=True)
            else:
                batch_id = await pipeline.start_session()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"batch_id": batch_id}


@router.post("/session/stop")
async def stop_session(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    async with site_control_lock(request.app):
        try:
            return await live_resources(request.app).pipeline.stop_session()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc


Box = tuple[float, float, float, float]


class CaptureAction(BaseModel):
    action: Literal["select", "retry", "skip", "fail", "freeze", "confirm", "manual"]
    revision: str
    awb_id: int | None = None
    boxes: tuple[Box, Box] | None = None


def _capture_images(frames):
    images = []
    for frame in frames:
        height, width = frame.shape[:2]
        scale = min(1, 960 / max(height, width))
        thumbnail = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
        ok, data = cv2.imencode(".jpg", thumbnail, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("Gagal menyiapkan preview capture.")
        images.append(base64.b64encode(data.tobytes()).decode("ascii"))
    return images


@router.get("/session/capture")
async def capture_state(request: Request, _: None = Depends(require_operator)):
    selection = live_resources(request.app).pipeline.capture_selection
    return selection.describe() if selection is not None else None


@router.get("/session/capture/preview")
async def capture_preview(revision: str, request: Request, _: None = Depends(require_operator)):
    selection = live_resources(request.app).pipeline.capture_selection
    if selection is None or revision != selection.revision:
        raise HTTPException(409, "Capture sudah berubah.")
    frames = selection.manual_frames if selection.state == "manual" else selection.candidate
    if frames is None:
        raise HTTPException(404, "Preview capture belum tersedia.")
    return JSONResponse(
        {"revision": revision, "images": await asyncio.to_thread(_capture_images, frames.images)},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/session/capture")
async def capture_action(body: CaptureAction, request: Request, _: None = Depends(require_operator)):
    async with site_control_lock(request.app):
        pipeline = live_resources(request.app).pipeline
        selection = pipeline.capture_selection
        if selection is None or selection.revision != body.revision or selection.state == "saving":
            raise HTTPException(409, "Capture sudah berubah. Muat ulang status.")
        try:
            if body.action == "select":
                items = await request.app.state.awb.waiting()
                if selection.revision != body.revision:
                    raise RuntimeError("Capture sudah berubah. Pilih ulang AWB.")
                awb = next((item for item in items if item.id == body.awb_id
                            and item.batch_id in (None, pipeline.session.batch_id)), None)
                if awb is None:
                    raise RuntimeError("AWB ini sudah tidak tersedia di antrean.")
                pipeline.select_capture_awb(awb)
            elif body.action == "skip":
                selection.clear()
            elif body.action == "retry":
                if selection.awb is None:
                    raise RuntimeError("Pilih AWB sebelum scan ulang.")
                selection.retry()
            elif body.action == "fail":
                if selection.state != "armed":
                    raise RuntimeError("Tidak ada capture yang sedang ditunggu.")
                selection.move("review", "Operator menandai capture gagal.")
            elif body.action == "freeze":
                pipeline.freeze_capture_frames()
            elif body.action == "manual":
                if body.boxes is None or any(not (0 <= l < r <= 1 and 0 <= t < b <= 1)
                                             for l, t, r, b in body.boxes):
                    raise HTTPException(422, "Pilih area yang valid di kedua tampak.")
                await pipeline.confirm_selected_capture(body.boxes)
            else:
                await pipeline.confirm_selected_capture()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return selection.describe()


@router.get("/session/state")
async def session_state(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    p = live_resources(request.app).pipeline
    try:
        pending = len(await request.app.state.store.pending_reviews())
    except Exception:  # noqa: BLE001 — store down: badge saja yang kosong
        pending = 0
    return {
        "running": p.session.running,
        "batch_id": p.session.batch_id,
        "parcel_count": p.session.parcel_count,
        "mode": request.app.state.settings.pipeline_mode,
        "pending_reviews": pending,
        "overlays": p.overlay_states(),
    }


@router.get("/session/batches")
async def session_batches(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    refs = await request.app.state.store.list_photo_references()
    counts: dict[str, dict[str, int]] = {}
    for ref in set(refs):
        batch_id, _, filename = ref.rpartition("/")
        match = re.fullmatch(r"seq_[0-9]+_(top|side)\.png", filename)
        if batch_id and match:
            views = counts.setdefault(batch_id, {"top": 0, "side": 0})
            views[match[1]] += 1
    return {"items": [{"batch_id": batch_id, "photo_count": sum(counts[batch_id].values()),
                       "xray_top": counts[batch_id]["top"], "xray_side": counts[batch_id]["side"]}
                      for batch_id in sorted(counts, reverse=True)]}


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    store = request.app.state.store
    db_ok = True
    try:
        await store.last_idx("__health__")
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "ok": db_ok,
        "model": request.app.state.worker.model_name,
        "model_version": request.app.state.worker.model_version,
        "dim": request.app.state.worker.dim,
    }
