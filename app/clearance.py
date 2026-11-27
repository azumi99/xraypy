"""Warehouse-scoped Clearance Type import and listing."""

import asyncio
from io import BytesIO

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response
from openpyxl import Workbook

from .labels import parse_workbook

router = APIRouter(prefix="/clearance-types", tags=["clearance-types"])
COLUMNS = ("mawb", "hawb", "clearance_type")


def _parse_rows(values):
    headers = [str(value or "").strip().lower() for value in next(values, ())]
    if any(headers.count(column) != 1 for column in COLUMNS):
        raise ValueError("Kolom wajib: mawb, hawb, clearance_type.")
    positions = [headers.index(column) for column in COLUMNS]
    records = {}
    for number, cells in enumerate(values, start=2):
        fields = tuple(str(cells[i]).strip() if i < len(cells) and cells[i] is not None else "" for i in positions)
        if not any(fields):
            continue
        if not all(fields):
            raise ValueError(f"Baris {number}: isi semua kolom.")
        mawb, hawb, clearance_type = fields
        records[mawb, hawb] = clearance_type
        if len(records) > 100_000:
            raise ValueError("Maksimal 100.000 pasangan MAWB dan HAWB per file.")
    if not records:
        raise ValueError("File belum berisi data.")
    return [(mawb, hawb, kind) for (mawb, hawb), kind in records.items()]


@router.get("")
async def list_clearance_types(request: Request):
    return await request.app.state.store.list_clearance_types()


@router.get("/template")
def clearance_template():
    with BytesIO() as output:
        workbook = Workbook()
        workbook.active.append(COLUMNS)
        workbook.save(output)
        workbook.close()
        return Response(
            output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="CLEARANCE TYPE.xlsx"'},
        )


@router.post("/import")
async def import_clearance_types(request: Request, file: UploadFile):
    try:
        rows = await asyncio.to_thread(parse_workbook, await file.read(), _parse_rows)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        await file.close()
    await request.app.state.store.import_clearance_types(rows)
    return {"saved": len(rows)}
