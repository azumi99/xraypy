import asyncio
import sqlite3
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from app.awb.router import router
from app.awb.service import AwbService
from app.events import EventBus
from app.dedup.sqlite_store import SqliteStore
from app.security import require_operator
from app.warehouse import warehouse_context


async def test_photo_upload_and_warehouse_isolation(store, settings, tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_operator] = lambda: None
    app.state.store = store
    app.state.awb = AwbService(settings, store)
    app.state.crop_dir = tmp_path / "crops"
    app.state.warehouse_runtimes = {site: SimpleNamespace(bus=EventBus()) for site in ("bgd", "cas")}
    events = {site: runtime.bus.subscribe() for site, runtime in app.state.warehouse_runtimes.items()}
    _, encoded = cv2.imencode(".jpg", np.zeros((4, 4, 3), dtype=np.uint8))
    data = encoded.tobytes()
    ids = {}
    for site in ("bgd", "cas"):
        token = warehouse_context.set(site)
        try:
            row = await store.insert_awb("MASTER", qr_scanned=True)
            ids[site] = (row.id, row.timecreated)
        finally:
            warehouse_context.reset(token)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for site, other in (("bgd", "cas"), ("cas", "bgd")):
            token = warehouse_context.set(site)
            try:
                awb_id, created = ids[site]
                url = f"/api/awb/{awb_id}/label-photo"
                files = {"file": ("../../hostile.jpg", data, "image/jpeg")}
                assert (await client.post(f"/api/awb/{ids[other][0]}/label-photo", files=files)).status_code == 404
                assert (await client.post(url, files={"file": ("x.jpg", b"\xff\xd8\xffbad", "image/jpeg")})).status_code == 422
                assert (await client.post(url, files={"file": ("x.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")})).status_code == 413
                _, png = cv2.imencode(".png", np.zeros((4, 4, 3), dtype=np.uint8))
                oversized = bytearray(png.tobytes())
                oversized[16:24] = struct.pack(">II", 7000, 4000)
                oversized[29:33] = struct.pack(">I", zlib.crc32(oversized[12:29]))
                assert (await client.post(url, files={"file": ("large.png", bytes(oversized), "image/png")})).status_code == 413
                original = store.attach_label_photo
                existing = set(app.state.crop_dir.rglob("*.jpg"))
                monkeypatch.setattr(store, "attach_label_photo", AsyncMock(side_effect=RuntimeError("db failure")))
                with pytest.raises(RuntimeError):
                    await client.post(url, files=files)
                assert set(app.state.crop_dir.rglob("*.jpg")) == existing
                monkeypatch.setattr(store, "attach_label_photo", original)
                responses = await asyncio.gather(client.post(url, files=files), client.post(url, files=files))
                assert sorted(r.status_code for r in responses) == [200, 409]
                assert events[site].get_nowait() == {"type": "awb.updated", "id": awb_id}
                assert events[other].empty()
                ref = next(r.json()["label_photo_ref"] for r in responses if r.status_code == 200)
                assert ref.startswith("cas/label-photos/" if site == "cas" else "label-photos/")
                assert (app.state.crop_dir / ref).read_bytes() == data
                assert (await client.get(f"/api/awb/{awb_id}")).json()["label_photo_ref"] == ref
                assert (await client.get(f"/api/awb/{awb_id}")).json()["timecreated"] == created
                assert (await client.get("/api/awb")).json()["items"][0]["label_photo_ref"] == ref
            finally:
                warehouse_context.reset(token)


async def test_photo_column_migration_preserves_rows(settings):
    with sqlite3.connect(settings.sqlite_path) as conn:
        for site in ("bgd", "cas"):
            conn.execute(f"CREATE TABLE awb_{site} (id INTEGER PRIMARY KEY AUTOINCREMENT, awb_number TEXT NOT NULL, batch_id TEXT, status TEXT DEFAULT 'WAITING', parcel_id INTEGER, paired_at TEXT, created_at TEXT NOT NULL)")
            conn.execute(f"INSERT INTO awb_{site}(awb_number,created_at) VALUES ('LEGACY','2020')")
    store = SqliteStore(settings)
    try:
        await store.initialize()
        await store.close()
        await store.initialize()
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                row = (await store.list_awbs())[0]
                assert row.awb_number == "LEGACY" and row.label_photo_ref is None and row.timecreated is None
            finally:
                warehouse_context.reset(token)
    finally:
        await store.close()


async def test_label_reference_is_not_attached_when_atomic_publish_fails(store, settings, tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_operator] = lambda: None
    app.state.store = store
    app.state.awb = AwbService(settings, store)
    app.state.crop_dir = tmp_path / "crops"
    app.state.warehouse_runtimes = {"bgd": SimpleNamespace(bus=EventBus())}
    token = warehouse_context.set("bgd")
    try:
        row = await store.insert_awb("MASTER", qr_scanned=True)
        _, encoded = cv2.imencode(".jpg", np.zeros((4, 4, 3), dtype=np.uint8))
        data = encoded.tobytes()

        def fail_publish(source, destination):
            assert source.read_bytes() == data
            assert not destination.exists()
            raise OSError("publish failed")

        monkeypatch.setattr("app.awb.router.os.link", fail_publish)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            with pytest.raises(OSError, match="publish failed"):
                await client.post(f"/api/awb/{row.id}/label-photo", files={"file": ("x.jpg", data, "image/jpeg")})
        assert (await store.list_awbs())[0].label_photo_ref is None
        assert not list(app.state.crop_dir.rglob("*.*"))
    finally:
        warehouse_context.reset(token)
