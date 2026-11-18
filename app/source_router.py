"""Per-view video source management (demo / screen share / cameras)."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from .security import require_operator
from .live import live_resources
from .warehouse import current_warehouse

router = APIRouter(tags=["sources"])


def _allowed_cameras(settings) -> list[str]:
    def parse(value: str) -> set[str]:
        parts = {item.strip() for item in value.split(",") if item.strip()}
        if any(not item.isascii() or not item.isdigit() for item in parts):
            raise HTTPException(status_code=503, detail="invalid warehouse camera configuration")
        return {str(int(item)) for item in parts}

    bgd = parse(settings.warehouse_bgd_cameras)
    cas = parse(settings.warehouse_cas_cameras)
    if bgd & cas:
        raise HTTPException(status_code=503, detail="camera assigned to multiple warehouses")
    return [f"camera:{index}" for index in sorted(bgd if current_warehouse() == "bgd" else cas, key=int)]


@router.get("/sources")
async def get_sources(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    mgr = live_resources(request.app).source_mgr
    return {**mgr.status(probe_cameras=False), "cameras": _allowed_cameras(request.app.state.settings)}


@router.post("/sources")
async def set_source(
    request: Request,
    body: dict[str, str],
    _: None = Depends(require_operator),
) -> dict[str, Any]:
    view = body.get("view")
    kind = body.get("kind", "")
    if view not in ("top", "side"):
        raise HTTPException(status_code=422, detail="view must be 'top' or 'side'")
    if kind.startswith("camera:") and kind not in _allowed_cameras(request.app.state.settings):
        raise HTTPException(status_code=403, detail="camera not assigned to this warehouse")
    try:
        await asyncio.to_thread(live_resources(request.app).source_mgr.set_kind, view, kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**live_resources(request.app).source_mgr.status(probe_cameras=False), "cameras": _allowed_cameras(request.app.state.settings)}
