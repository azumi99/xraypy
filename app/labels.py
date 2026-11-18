"""Label import and authenticated endpoints."""

from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from openpyxl import load_workbook
import xlrd
from pydantic import BaseModel

from .security import require_operator

router = APIRouter(prefix="/labels", tags=["labels"], dependencies=[Depends(require_operator)])


class DeleteLabels(BaseModel):
    ids: list[int]


def _count(value, name):
    if value is None or str(value).strip() == "" or value == 0:
        return None
    number = float(value)
    if not number.is_integer() or number < 0 or number > 2147483647:
        raise ValueError(f"Invalid {name}")
    return int(number)


def _text(value):
    return str(value).strip() if value is not None and str(value).strip() else None


def _parse_rows(values) -> list[tuple]:
    headers = [str(value or "").strip().lower() for value in next(values, ())]
    if "mawb" not in headers or "hawb" not in headers:
        raise ValueError("Workbook must contain mawb and hawb headers")
    rows = []
    for cells in values:
        row = dict(zip(headers, cells, strict=False))
        mawb = str(row.get("mawb") or "").strip()
        hawb = str(row.get("hawb") or "").strip()
        if not mawb and not hawb:
            continue
        pcs_mawb = _count(row.get("pcs_mawb"), "pcs_mawb")
        pcs_hawb = _count(row.get("pcs_hawb"), "pcs_hawb")
        flight = _text(row.get("flightno")) or _text(row.get("flight_no"))
        if len(rows) + (pcs_hawb or 1) > 100_000:
            raise ValueError("Workbook expands to more than 100000 labels")
        for part in range(1, (pcs_hawb or 1) + 1):
            rows.append((mawb, pcs_mawb, hawb, pcs_hawb, part,
                         _text(row.get("airlines_name")), flight, _text(row.get("route"))))
    return rows


def parse_workbook(data: bytes) -> list[tuple]:
    try:
        if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            workbook = xlrd.open_workbook(file_contents=data)
            try:
                sheet = workbook.sheet_by_index(0)
                return _parse_rows(iter(sheet.row_values(i) for i in range(sheet.nrows)))
            finally:
                workbook.release_resources()
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            return _parse_rows(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
    except Exception as exc:
        raise ValueError(f"Invalid Excel workbook: {exc}") from exc


@router.get("")
async def list_labels(request: Request):
    return await request.app.state.store.list_labels()


@router.post("/import")
async def import_labels(request: Request, file: Annotated[UploadFile, File()]):
    try:
        rows = parse_workbook(await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()
    return await request.app.state.store.import_labels(rows)


@router.post("/delete-many")
async def delete_labels(body: DeleteLabels, request: Request):
    return {"deleted": await request.app.state.store.delete_labels(body.ids)}
