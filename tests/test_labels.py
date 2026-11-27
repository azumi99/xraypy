from io import BytesIO
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import Workbook

from app.labels import router
from app.security import require_operator
from app.dedup.sqlite_store import SqliteStore, _SCHEMA


def workbook_bytes(pcs=2, route="CGK-SIN", *, mawb="M1", hawb="H1"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([" HAWB ", "MAWB", "PCS_HAWB", "FlightNo", "route"])
    sheet.append([hawb, mawb, pcs, "FX1", route])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


@pytest.mark.asyncio
async def test_labels_api(store, settings):
    settings.stream_token = "secret"
    app = FastAPI()
    app.state.store = store
    app.state.settings = settings
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/labels")).status_code == 401
        app.dependency_overrides[require_operator] = lambda: None
        headers = {"x-operator-token": "secret"}
        assert (await client.post("/api/labels/import", headers=headers, files={"file": ("bad.xlsx", b"bad")})).status_code == 400
        assert (await client.post("/api/labels/import", headers=headers, files={"file": ("large.xlsx", workbook_bytes(100001))})).status_code == 400
        response = await client.post("/api/labels/import", headers=headers, files={"file": ("labels.xlsx", workbook_bytes())})
        assert response.json() == {"imported": 2, "updated": 0, "skipped": 0}
        response = await client.post("/api/labels/import", headers=headers, files={"file": ("labels.xlsx", workbook_bytes(3, "SIN-HKG"))})
        assert response.json() == {"imported": 1, "updated": 2, "skipped": 2}
        labels = (await client.get("/api/labels", headers=headers)).json()
        assert [item["part"] for item in labels] == [3, 2, 1]
        assert all(item["route"] == "SIN-HKG" and item["flight_no"] == "FX1" for item in labels)
        assert (await client.post("/api/labels/delete-many", headers=headers, json={"ids": [labels[0]["id"], 99999]})).json() == {"deleted": 1}
        assert len((await client.get("/api/labels", headers=headers)).json()) == 2


@pytest.mark.asyncio
async def test_sqlite_label_rename_preserves_existing_data(tmp_path, settings):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA.replace("label_print", "label_fedex"))
        conn.execute("INSERT INTO label_fedex (mawb, hawb, part) VALUES (?, ?, ?)", ("M1", "H1", 1))
    store = SqliteStore(settings, path)
    try:
        await store.initialize()
        labels = await store.list_labels()
        assert [(row["mawb"], row["hawb"], row["part"]) for row in labels] == [("M1", "H1", 1)]
        assert store._connect().execute("SELECT name FROM sqlite_master WHERE name='label_fedex'").fetchone() is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_hawb_column_migrates_existing_awbs(tmp_path, settings):
    path = tmp_path / "legacy-awb.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA.replace("    hawb_number TEXT,\n", ""))
        conn.execute("INSERT INTO awb (awb_number, created_at) VALUES (?, ?)", ("AWB123", "2026-01-01"))
    store = SqliteStore(settings, path)
    try:
        await store.initialize()
        row = store._connect().execute("SELECT awb_number, hawb_number FROM awb").fetchone()
        assert (row["awb_number"], row["hawb_number"]) == ("AWB123", None)
    finally:
        await store.close()
