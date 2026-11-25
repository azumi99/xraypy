"""REST endpoints for AWB queue management."""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Annotated, Any, Literal

import cv2
import jwt
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

from ..live import live_resources
from ..photo_delivery import deliver_photo
from ..security import identity, require_operator
from ..warehouse import current_warehouse
from .manifest import fetch_manifest, mawb_candidates
from .service import AwbPairedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/awb", tags=["awb"])


class AwbBody(BaseModel):
    awb_number: str | None = Field(default=None, min_length=3, max_length=128)
    qr_code: str | None = Field(default=None, min_length=1, max_length=128)
    batch_id: str | None = None
    hawb_number: str | None = Field(default=None, min_length=1, max_length=64)
    part: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    total: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    proof_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_input(self):
        if self.qr_code is not None and self.awb_number is not None:
            raise ValueError("Send qr_code or awb_number, not both")
        # Older mobile clients put the complete QR payload in awb_number.
        raw = self.qr_code if self.qr_code is not None else self.awb_number
        if raw is None or not raw.strip():
            raise ValueError("qr_code or awb_number is required")
        raw = raw.strip()
        if self.qr_code is not None or raw.count("_") >= 2:
            pieces = raw.rsplit("_", 2)
            if (len(pieces) != 3 or not pieces[0].strip() or len(pieces[0]) > 64
                    or any(not value.isascii() or not value.isdecimal() for value in pieces[1:])):
                raise ValueError("QR must use HAWB_PART_JUMLAH with positive integer Part and Total")
            hawb, part, total = pieces[0], int(pieces[1]), int(pieces[2])
            if not 1 <= part <= total <= 2147483647:
                raise ValueError("QR requires 1 <= Part <= Total <= 2147483647")
            for name, value in (("hawb_number", hawb), ("part", part), ("total", total)):
                if getattr(self, name) is not None and getattr(self, name) != value:
                    raise ValueError(f"{name} conflicts with QR payload")
                setattr(self, name, value)
            self.qr_code = raw
            self.awb_number = None
        else:
            if not 3 <= len(raw) <= 64:
                raise ValueError("awb_number must contain 3 to 64 characters")
            self.awb_number = raw
        if (self.part is None) != (self.total is None):
            raise ValueError("Part and Total must be supplied together")
        if self.part is not None and self.part > self.total:
            raise ValueError("Part must not exceed Total")
        if self.hawb_number is not None and not self.hawb_number.strip():
            raise ValueError("hawb_number must not be blank")
        if self.proof_token is not None and self.qr_code is None:
            raise ValueError("proof_token requires a QR payload")
        return self


class QrCheckBody(BaseModel):
    qr_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_qr(self):
        AwbBody(qr_code=self.qr_code)
        return self


async def _dump_detail(request: Request, a) -> dict[str, Any]:
    """AWB record + paired parcel details (idx, status, crop for the photo)."""
    rec = await request.app.state.store.get(a.parcel_id) if a.parcel_id else None
    return _dump_with_parcel(a, rec)


def _dump_with_parcel(a, rec) -> dict[str, Any]:
    d = _dump(a)
    if rec is not None:
        created = rec.created_at.isoformat() if rec.created_at else None
        d["parcel"] = {
            "id": rec.id,
            "idx": rec.idx,
            "status": rec.status,
            "batch_id": rec.batch_id,
            "track_id": rec.track_id,
            "crop_ref": rec.crop_ref,
            "crop_ref_side": rec.crop_ref_side,
            "sim_score": rec.sim_score,
            "created_at": created,
        }
    return d


async def _dump_list(request: Request, items) -> dict[str, Any]:
    ids = list({a.parcel_id for a in items if a.parcel_id})
    parcels = await request.app.state.store.get_many_metadata(ids) if ids else {}
    return {"items": [_dump_with_parcel(a, parcels.get(a.parcel_id)) for a in items]}


async def _resolve_qr(body: AwbBody, request: Request):
    mawbs = await mawb_candidates(current_warehouse(), body.hawb_number)
    if mawbs is None:
        raise HTTPException(503, "Manifest tidak tersedia. Periksa koneksi lalu coba lagi.")
    source = "manifest"
    if not mawbs:
        mawbs = await request.app.state.store.label_mawbs(body.hawb_number)
        source = "label"
    if not mawbs:
        raise HTTPException(422, f"HAWB {body.hawb_number!r} tidak ditemukan di manifest atau label gudang ini")
    if len(mawbs) != 1:
        raise HTTPException(422, f"HAWB {body.hawb_number!r} maps to multiple MAWBs in this warehouse's {source}; resolve the ambiguous data")
    mawb = mawbs[0].strip()
    if not mawb:
        raise HTTPException(422, f"HAWB {body.hawb_number!r} has no usable MAWB in this warehouse's {source}")
    return mawb, await _find_qr_match(body, request, mawb), source


async def _find_qr_match(body: AwbBody, request: Request, mawb: str):
    existing = [
        a for a in await request.app.state.awb.all()
        if a.awb_number == mawb and a.hawb_number == body.hawb_number and a.part == body.part
    ]
    if len(existing) > 1:
        raise HTTPException(409, "Multiple AWBs match this MAWB, HAWB and part; resolve the duplicate records")
    return existing[0] if existing else None


@router.post("/qr-check")
async def check_qr(body: QrCheckBody, request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    user = identity(request)
    if not user or not user.get("email"):
        raise HTTPException(401, "authentication required")
    parsed = AwbBody(qr_code=body.qr_code)
    mawb, match, source = await _resolve_qr(parsed, request)
    now = int(time.time())
    proof = jwt.encode({"purpose": "awb-qr-proof", "sub": user["email"],
                        "warehouse": current_warehouse(), "qr": parsed.qr_code,
                        "mawb": mawb, "iat": now, "exp": now + 300},
                       request.app.state.settings.jwt_secret, algorithm="HS256")
    return {"existing": match is not None, "mawb": mawb, "hawb_number": parsed.hawb_number,
            "part": parsed.part, "total": parsed.total, "proof_token": proof, "source": source,
            "awb": await _dump_detail(request, match) if match else None}


@router.post("")
async def insert_awb(body: AwbBody, request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    service = request.app.state.awb
    mawb = body.awb_number
    if body.qr_code is not None:
        async with service.qr_locks[current_warehouse()]:
            if body.proof_token is not None:
                user = identity(request)
                if not user or not user.get("email"):
                    raise HTTPException(401, "authentication required")
                try:
                    claims = jwt.decode(body.proof_token, request.app.state.settings.jwt_secret,
                                        algorithms=["HS256"], options={"require": ["purpose", "sub", "warehouse", "qr", "mawb", "iat", "exp"]})
                    if (claims["purpose"] != "awb-qr-proof" or claims["sub"] != user["email"]
                            or claims["warehouse"] != current_warehouse() or claims["qr"] != body.qr_code
                            or not isinstance(claims["mawb"], str) or not claims["mawb"].strip()
                            or claims["mawb"] != claims["mawb"].strip()
                            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
                            or not 0 < claims["exp"] - claims["iat"] <= 300):
                        raise ValueError("invalid proof claims")
                    mawb = claims["mawb"]
                except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
                    raise HTTPException(422, "QR proof invalid or expired; run qr-check again and retry") from None
                match = await _find_qr_match(body, request, mawb)
            else:
                mawb, match, _ = await _resolve_qr(body, request)
            if match:
                out = await _dump_detail(request, match)
                out["existing"] = True
                return out
            record = await service.insert(mawb, body.batch_id, body.hawb_number, body.part, body.total, qr_scanned=True)
    else:
        record = await service.insert(mawb, body.batch_id, body.hawb_number, body.part, body.total)
    await live_resources(request.app).bus.publish("awb.inserted", {"awb": _dump(record)})
    return await _dump_detail(request, record)


@router.get("/waiting")
async def list_waiting(request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    service = request.app.state.awb
    items = await service.waiting()
    return await _dump_list(request, items)


MatrixText = Annotated[str, Field(max_length=1024)]
MATRIX_EXPORT_COLUMNS = (
    "MAWB", "HAWB", "Pieces", "Waktu dibuat di manifest (WIB)",
    "Waktu foto X-ray terakhir (WIB)", "Waktu kirim CEISA (WIB)",
)


class MatrixExportRow(BaseModel):
    mawb: MatrixText
    hawb: MatrixText
    pieces: int | None
    manifest_at: MatrixText
    xray_at: MatrixText


class MatrixExportBody(BaseModel):
    format: Literal["csv", "xlsx"]
    rows: list[MatrixExportRow] = Field(max_length=20000)


@router.post("/matrix/export")
def export_matrix(body: MatrixExportBody, _: None = Depends(require_operator)) -> Response:
    rows = [MATRIX_EXPORT_COLUMNS, *(
        (row.mawb, row.hawb, row.pieces, row.manifest_at, row.xray_at, "") for row in body.rows
    )]
    headers = {"Content-Disposition": f'attachment; filename="matrix.{body.format}"', "Cache-Control": "no-store"}
    if body.format == "csv":
        output = StringIO(newline="")
        writer = csv.writer(output)
        for row in rows:
            writer.writerow([
                "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value for value in row
            ])
        return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv", headers=headers)

    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Matrix")
    for row in rows:
        cells = [WriteOnlyCell(sheet, value=value) for value in row]
        for cell, value in zip(cells, row):
            if isinstance(value, str):
                cell.data_type = "s"
        sheet.append(cells)
    output = BytesIO()
    workbook.save(output)
    return Response(output.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)


@router.get("/{awb_id}")
async def get_awb(awb_id: int, request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    """Detail satu AWB + parcel terpasang (untuk halaman /awb/:id)."""
    service = request.app.state.awb
    all_awbs = await service.all()
    target = next((a for a in all_awbs if a.id == awb_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="AWB not found")
    out = await _dump_detail(request, target)
    if target.parcel_id:
        out["audit"] = await request.app.state.store.list_audit(target.parcel_id)
    return out


def _render_xray_frame(source: Path | bytes, detail: dict[str, Any], view: str) -> bytes:
    with Image.open(source if isinstance(source, Path) else BytesIO(source)) as original:
        if original.width * original.height > 20_000_000:
            raise ValueError("Photo is too large")
        photo = original.convert("RGB")
    width = max(640, photo.width)
    height = round(photo.height * width / photo.width)
    if width * height > 20_000_000:
        raise ValueError("Photo is too large")
    scale = width / max(320, photo.width)
    bar, header = round(14 * scale), round(33 * scale)
    line = max(1, round(2 * scale))
    pad = round(8 * scale)
    font_dir = Path(__file__).resolve().parents[1] / "assets/poppins"
    font_size = max(6, round(6 * scale))
    regular = ImageFont.truetype(str(font_dir / "Poppins-Regular.ttf"), font_size)
    bold = ImageFont.truetype(str(font_dir / "Poppins-Bold.ttf"), font_size)
    canvas = Image.new("RGB", (width, height + header), "#f8fafb")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width - 1, bar - 1), fill="#152630")
    draw.rectangle((0, header - line, width - 1, header - 1), fill="#d8ab51")

    def fit(text: str, limit: int, font: ImageFont.FreeTypeFont) -> str:
        if draw.textlength(text, font=font) <= limit:
            return text
        while text and draw.textlength(text + "…", font=font) > limit:
            text = text[:-1]
        return text + "…"

    caption = "Tampak Samping" if view == "side" else "Tampak Atas"
    captured = detail["parcel"].get("created_at")
    if captured:
        timestamp = datetime.fromisoformat(captured).astimezone(timezone(timedelta(hours=7)))
        caption += timestamp.strftime(" · %d/%m/%Y %H:%M WIB")
    draw.text((pad, bar // 2), "Xray Cargo", fill="#f2f6fa", font=bold, anchor="lm")
    draw.text((width - pad, bar // 2), fit(caption, width - round(74 * scale), regular),
              fill="#f2cb79", font=regular, anchor="rm")
    fields = (
        ("HAWB", detail.get("hawb_number") or "—"),
        ("MAWB", detail.get("awb_number") or "—"),
        ("Part", f"{detail.get('part') or '—'} / {detail.get('total') or '—'}"),
    )
    column = width // len(fields)
    for index, (label, value) in enumerate(fields):
        left = index * column
        if index:
            draw.line((left, bar, left, header - line - 1), fill="#cbd4da")
        draw.text((left + pad, bar + round(5 * scale)), label, fill="#34434c", font=regular, anchor="lm")
        draw.text((left + pad, bar + round(13 * scale)), fit(value, column - 2 * pad, bold),
                  fill="#152630", font=bold, anchor="lm")
    canvas.paste(photo.resize((width, height), Image.Resampling.LANCZOS), (0, header))
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


@router.get("/{awb_id}/xray-preview/{view}")
async def get_xray_preview(
    awb_id: int, view: Literal["top", "side"], request: Request, _: None = Depends(require_operator),
) -> Response:
    """Render a light, shareable X-ray preview for an AWB in the current warehouse."""
    detail = await get_awb(awb_id, request)
    reference = (detail.get("parcel") or {}).get("crop_ref_side" if view == "side" else "crop_ref")
    if not reference:
        raise HTTPException(404, "Photo not found")
    photo = await deliver_photo(request, reference)
    source = Path(photo.path) if isinstance(photo, FileResponse) else photo.body
    try:
        data = await asyncio.to_thread(_render_xray_frame, source, detail, view)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(503, "Preview unavailable") from None
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/{awb_id}/label-photo")
async def upload_label_photo(
    awb_id: int, request: Request, file: UploadFile, _: None = Depends(require_operator)
) -> dict[str, Any]:
    target = next((a for a in await request.app.state.awb.all() if a.id == awb_id), None)
    if target is None:
        raise HTTPException(404, "AWB not found")
    if target.label_photo_ref:
        raise HTTPException(409, "Label photo already exists")
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 10 MiB")
    if file.content_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif file.content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    else:
        raise HTTPException(422, "Expected a JPEG or PNG image")
    try:
        with Image.open(BytesIO(data)) as header:
            if header.width * header.height > 24_000_000:
                raise HTTPException(413, "Image exceeds 24 megapixels")
            if header.format != ("JPEG" if ext == "jpg" else "PNG"):
                raise HTTPException(422, "Image format does not match content type")
            header.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Invalid image") from None
    try:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        image = None
    if image is None or not image.size:
        raise HTTPException(422, "Invalid image")
    prefix = "cas/" if current_warehouse() == "cas" else ""
    ref = f"{prefix}label-photos/{uuid.uuid4().hex}.{ext}"
    root = Path(request.app.state.crop_dir).resolve()
    path = root / ref
    if not path.parent.resolve().is_relative_to(root):
        raise HTTPException(500, "Invalid storage directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    attached = False
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".label-", delete=False) as output:
            temp_path = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.link(temp_path, path)
                created = True
            finally:
                temp_path.unlink(missing_ok=True)
        update = asyncio.create_task(request.app.state.store.attach_label_photo(awb_id, ref))
        try:
            attached = await asyncio.shield(update)
        except asyncio.CancelledError:
            attached = await update
            raise
        if not attached:
            exists = any(a.id == awb_id for a in await request.app.state.awb.all())
            raise HTTPException(409 if exists else 404, "Label photo already exists" if exists else "AWB not found")
    except BaseException:
        if created and not attached:
            path.unlink(missing_ok=True)
        raise
    target.label_photo_ref = ref
    sync = getattr(request.app.state, "photo_sync", None)
    if sync is not None and sync.enabled:
        try:
            await asyncio.to_thread(sync.enqueue, current_warehouse(), ref)
        except Exception:  # noqa: BLE001 — photo is already attached; do not report a failed upload
            logger.exception("failed to enqueue finalized label photo")
    await live_resources(request.app).bus.publish("awb.updated", {"id": awb_id})
    return await _dump_detail(request, target)


@router.get("/{awb_id}/manifest")
async def get_manifest(awb_id: int, request: Request, _: None = Depends(require_operator)) -> dict[str, Any]:
    target = next((a for a in await request.app.state.awb.all() if a.id == awb_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="AWB not found")
    return await fetch_manifest(identity(request)["access"], target.awb_number, target.hawb_number)


@router.get("")
async def list_all(
    status: str | None = None, request: Request = None, _: None = Depends(require_operator)
) -> dict[str, Any]:  # type: ignore[assignment]
    service = request.app.state.awb
    items = await service.all(status)
    return await _dump_list(request, items)


@router.delete("/{awb_id}")
async def delete_awb(
    awb_id: int,
    force: bool = False,
    request: Request = None,  # type: ignore[assignment]
    _: None = Depends(require_operator),
) -> dict[str, Any]:
    """Hapus permanen AWB. WAITING/CANCELLED langsung terhapus; PAIRED hanya
    dengan ?force=true — ikatan ke paket dilepas (paket jadi tanpa AWB) dan
    setiap paket yang dilepas tercatat di cv.review_audit."""
    service = request.app.state.awb
    try:
        deleted = await service.delete(awb_id, force=force)
    except AwbPairedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="AWB not found")
    await live_resources(request.app).bus.publish("awb.deleted", {"id": awb_id, "force": force})
    return {"id": awb_id, "deleted": True, "force": force}


def _dump(a) -> dict[str, Any]:
    created_at = a.created_at
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    timecreated = a.timecreated
    if hasattr(timecreated, "isoformat"):
        timecreated = timecreated.isoformat()
    return {
        "id": a.id,
        "awb_number": a.awb_number,
        "hawb_number": a.hawb_number,
        "part": a.part,
        "total": a.total,
        "label_photo_ref": a.label_photo_ref,
        "timecreated": timecreated,
        "batch_id": a.batch_id,
        "status": a.status,
        "parcel_id": a.parcel_id,
        "created_at": created_at,
    }
