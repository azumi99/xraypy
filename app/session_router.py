"""Session control + service status endpoints."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .security import require_operator
from .live import live_resources, site_control_lock

router = APIRouter(tags=["session"])


@router.post("/session/start")
async def start_session(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    async with site_control_lock(request.app):
        pipeline = live_resources(request.app).pipeline
        try:
            batch_id = await pipeline.start_session()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"batch_id": batch_id}


@router.post("/session/stop")
async def stop_session(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    async with site_control_lock(request.app):
        return await live_resources(request.app).pipeline.stop_session()


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
