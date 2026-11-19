"""Warehouse-scoped monitoring and control of manifest SSH tunnels."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .security import require_operator
from .live import site_control_lock
from .tunnel_manager import manager
from .warehouse import current_warehouse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/tunnels")
async def tunnel_status(_: None = Depends(require_operator)):
    site = current_warehouse()
    return JSONResponse({"warehouse": site, "manifest": manager.status(site), "database": manager.status("db")},
                        headers={"Cache-Control": "no-store"})


@router.post("/tunnels/manifest/{action}")
async def control_manifest(action: Literal["start", "stop", "restart"], request: Request,
                           _: None = Depends(require_operator)):
    site = current_warehouse()
    async with site_control_lock(request.app):
        status = manager.status(site)
        if not status["enabled"]:
            raise HTTPException(409, "Tunnel manifest belum dikonfigurasi")
        runtimes = getattr(request.app.state, "warehouse_runtimes", {})
        runtime = runtimes.get(site)
        pipeline = runtime.pipeline if runtime else getattr(request.app.state, "pipeline", None) if site == "bgd" else None
        if action in ("stop", "restart") and pipeline is not None and pipeline.session.running:
            raise HTTPException(409, "Hentikan sesi scan sebelum menghentikan atau me-restart tunnel")
        getattr(manager, action)(site)
    return {"warehouse": site, "manifest": manager.status(site)}


@router.get("/sync")
async def sync_monitoring(request: Request, _: None = Depends(require_operator)):
    from .warehouse import warehouse_context

    sync = getattr(request.app.state, "photo_sync", None)
    result = {"enabled": bool(sync and sync.enabled), "items": {}}
    if not result["enabled"]:
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    for site in ("bgd", "cas"):
        token = warehouse_context.set(site)
        try:
            refs = await request.app.state.store.list_photo_references()
        finally:
            warehouse_context.reset(token)
        statuses = await asyncio.to_thread(sync.status, site, refs)
        summary = {"synced": 0, "pending": 0, "failed": 0, "unknown": 0, "total": len(refs)}
        for value in statuses.values():
            summary[value] = summary.get(value, 0) + 1
        result["items"][site] = summary
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
