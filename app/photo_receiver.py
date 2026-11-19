"""Authenticated, tenant-isolated receiver for finalized photos."""

import asyncio
import errno
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import stat
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError

from .photo_sync import PhotoSync
from .warehouse import warehouse_context

router = APIRouter(tags=["photo sync"])
MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 40_000_000


def _io_limit(app):
    if not hasattr(app.state, "photo_sync_io_limit"):
        app.state.photo_sync_io_limit = asyncio.Semaphore(2)
    return app.state.photo_sync_io_limit


def _read_checked(root, site, ref):
    data = read_photo(root, site, ref)
    return data, photo_mime(data)


def read_photo(root, site, ref):
    reader = SimpleNamespace(root=Path(root), _parts=PhotoSync._parts)
    data = PhotoSync._read(reader, site, ref)
    if len(data) > MAX_BYTES:
        raise ValueError("Photo too large")
    return data


def photo_stored(root, site, ref):
    """Check a tenant-scoped file without reading or following symlinks."""
    parts = PhotoSync._parts(site, ref)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            return stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_BYTES
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def photo_mime(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ("PNG", "JPEG"):
            raise ValueError("Unsupported image format")
        return Image.MIME[image.format]


def _read_digest(root, site, ref):
    data = read_photo(root, site, ref)
    return hashlib.sha256(data).hexdigest()


@router.get("/api/sync/photos")
async def get_photo(request: Request, ref: str, digest: str = "", authorization: Annotated[str | None, Header()] = None):
    site = _site(request.app.state.settings, authorization)
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
    if digest == "1":
        async with _io_limit(request):
            try:
                value = await asyncio.to_thread(_read_digest, request.app.state.settings.crop_dir, site, ref)
            except (OSError, ValueError):
                raise HTTPException(404, "Photo not found") from None
        return JSONResponse({"sha256": value})
    try:
        async with _io_limit(request.app):
            data, mime = await asyncio.to_thread(_read_checked, request.app.state.settings.crop_dir, site, ref)
    except (OSError, ValueError, UnidentifiedImageError):
        raise HTTPException(404, "Photo not found") from None
    return Response(data, media_type=mime, headers={"X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                                                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _site(settings, authorization):
    configured = getattr(settings, "photo_sync_devices_json", "")
    if configured:
        try:
            devices = json.loads(configured)
            if not isinstance(devices, dict) or not all(
                isinstance(name, str) and isinstance(value, dict) and value.get("site") in ("bgd", "cas")
                and isinstance(value.get("token"), str) and len(value["token"]) >= 32
                for name, value in devices.items()
            ):
                raise ValueError("invalid device configuration")
            tokens = [(value["site"], value["token"]) for value in devices.values()]
        except (TypeError, ValueError):
            raise HTTPException(503, "Photo sync device configuration is invalid") from None
    else:
        tokens = []
    tokens = [(site, token) for site, token in tokens if isinstance(token, str) and token]
    if not tokens:
        raise HTTPException(503, "Photo sync is disabled")
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    # Compare both configured tokens even after a match, and fail closed on ambiguity.
    matches = [site for site, token in tokens if hmac.compare_digest(token.encode(), supplied.encode())]
    if len(matches) != 1 or not supplied:
        raise HTTPException(401, "Invalid credentials")
    return matches[0]


def _publish(root, parts, data, digest):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, flags)
    temp = None
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        name = ".photo-sync-" + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        temp = name
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temp, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
            return True
        except FileExistsError:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as existing:
                info = os.fstat(existing.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise HTTPException(409, "Photo target is not a regular file") from None
                if info.st_size > MAX_BYTES:
                    raise HTTPException(409, "Photo already exists with different content") from None
                actual = hashlib.file_digest(existing, "sha256").hexdigest()
            if not hmac.compare_digest(actual, digest):
                raise HTTPException(409, "Photo already exists with different content") from None
            return False
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


@router.put("/api/sync/photos")
async def receive_photo(request: Request, ref: Annotated[str, Form()], sha256: Annotated[str, Form()],
                        file: Annotated[UploadFile, File()], authorization: Annotated[str | None, Header()] = None):
    site = _site(request.app.state.settings, authorization)
    parts = ref.split("/")
    if (not ref or "\\" in ref or any(p in ("", ".", "..") or "\x00" in p for p in parts)
            or (site == "cas" and (parts[0] != "cas" or len(parts) < 2))
            or (site == "bgd" and parts[0].casefold() == "cas")):
        raise HTTPException(400, "Invalid photo reference")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise HTTPException(400, "Invalid digest")
    token = warehouse_context.set(site)
    try:
        exists = await request.app.state.store.photo_reference_exists(ref)
    finally:
        warehouse_context.reset(token)
    if not exists:
        raise HTTPException(404, "Photo reference not found")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Photo too large")
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, sha256.lower()):
        raise HTTPException(400, "Digest mismatch")
    async with _io_limit(request.app):
        created = await asyncio.to_thread(_validate_and_publish, request.app.state.settings.crop_dir, parts, data, digest)
    return {"status": "created" if created else "exists", "sha256": digest}


def _validate_and_publish(root, parts, data, digest):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG"):
                    raise ValueError("Unsupported image format")
                if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS:
                    raise ValueError("Invalid dimensions")
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(400, "Invalid image") from None
    try:
        return _publish(root, parts, data, digest)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR):
            raise HTTPException(400, "Unsafe photo path") from None
        raise
