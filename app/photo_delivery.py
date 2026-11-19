"""Authenticated, local-first delivery of database-referenced photos."""

import asyncio
import hashlib
import hmac
import os
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import UnidentifiedImageError

from .photo_receiver import MAX_BYTES, photo_mime, read_photo
from .photo_sync import PhotoSync
from .security import identity
from .warehouse import warehouse_context

router = APIRouter(tags=["photo delivery"])
_WINDOWS = os.name == "nt"


async def _remote(sync, site, ref):
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", sync.url, params={"ref": ref},
                                 headers={"Authorization": f"Bearer {sync.tokens[site]}"}) as response:
            if response.status_code == 404:
                raise HTTPException(404, "Photo not found")
            if response.status_code != 200:
                raise HTTPException(503, "Photo source unavailable")
            digest = response.headers.get("X-Content-SHA256", "")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise HTTPException(503, "Photo source unavailable")
            data = bytes(data)
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
                raise HTTPException(503, "Photo source unavailable")
            return data


@router.get("/api/sync/photo")
async def deliver_photo(request: Request, ref: str):
    user = identity(request)
    if not user:
        raise HTTPException(401, "authentication required")
    site = user["access"]
    try:
        PhotoSync._parts(site, ref)
    except ValueError:
        raise HTTPException(404, "Photo not found") from None
    token = warehouse_context.set(site)
    try:
        exists = await request.app.state.store.photo_reference_exists(ref)
    finally:
        warehouse_context.reset(token)
    if not exists:
        raise HTTPException(404, "Photo not found")
    if _WINDOWS:
        # Use the existing StaticFiles-style resolved-path check for local
        # Windows previews; automatic synchronization remains disabled there.
        root = Path(request.app.state.settings.crop_dir).resolve()
        path = (root / ref).resolve()
        if path.is_relative_to(root) and path.is_file():
            return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        raise HTTPException(404, "Photo not found")
    try:
        data = await asyncio.to_thread(read_photo, request.app.state.settings.crop_dir, site, ref)
    except FileNotFoundError:
        sync = request.app.state.photo_sync
        if not sync.enabled:
            raise HTTPException(404, "Photo not found") from None
        try:
            data = await asyncio.wait_for(_remote(sync, site, ref), timeout=15)
        except (httpx.HTTPError, TimeoutError, KeyError):
            raise HTTPException(503, "Photo source unavailable") from None
    except (OSError, ValueError):
        raise HTTPException(404, "Photo not found") from None
    try:
        mime = photo_mime(data)
    except (OSError, ValueError, UnidentifiedImageError):
        raise HTTPException(503, "Photo unavailable") from None
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
