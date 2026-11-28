"""Application settings and warehouse-scoped monitoring."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import XrayMachineConfig
from .security import require_operator
from .live import site_control_lock
from .tunnel_manager import manager
from .warehouse import current_warehouse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/xray-machine")
async def xray_machine(request: Request, _: None = Depends(require_operator)):
    data = await request.app.state.store.get_xray_device(current_warehouse())
    config = request.app.state.settings.xray_machine if data is None else XrayMachineConfig.model_validate_json(data)
    return JSONResponse(config.model_dump(by_alias=True), headers={"Cache-Control": "no-store"})


@router.put("/xray-machine")
async def save_xray_machine(config: XrayMachineConfig, request: Request, _: None = Depends(require_operator)):
    if config.model_fields_set != set(XrayMachineConfig.model_fields):
        raise HTTPException(422, "Lengkapi semua field data mesin X-ray.")
    await request.app.state.store.save_xray_device(current_warehouse(), config.model_dump_json(by_alias=True))
    return JSONResponse(config.model_dump(by_alias=True), headers={"Cache-Control": "no-store"})


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
    sync = getattr(request.app.state, "photo_sync", None)
    result = {"enabled": bool(sync and sync.enabled), "items": {}}
    if not result["enabled"]:
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    site = current_warehouse()
    refs = await request.app.state.store.list_photo_references()
    statuses = await asyncio.to_thread(sync.status, site, refs)
    summary = {"synced": 0, "available": 0, "pending": 0, "failed": 0, "unknown": 0, "total": len(refs)}
    for value in statuses.values():
        summary[value] = summary.get(value, 0) + 1
    result["items"][site] = summary
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
