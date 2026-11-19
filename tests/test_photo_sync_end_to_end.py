import asyncio
import json
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from app.config import DedupConfig, Settings
from app.dedup.sqlite_store import SqliteStore
from app.photo_receiver import router
from app.photo_sync import PhotoSync, mark_final_png
from app.warehouse import warehouse_context


@pytest.mark.asyncio
async def test_referenced_qr_and_final_capture_sync_for_each_warehouse(tmp_path, monkeypatch):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    bgd_token, cas_token = "b" * 40, "c" * 40
    settings = Settings(_env_file=None, sqlite_path=str(tmp_path / "shared.sqlite"), crop_dir=local,
                        dedup=DedupConfig(store="sqlite"), photo_sync_enabled=True,
                        photo_sync_url="https://receiver.test", photo_sync_outbox_path=tmp_path / "outbox.sqlite",
                        photo_sync_bgd_token=bgd_token, photo_sync_cas_token=cas_token)
    store = SqliteStore(settings)
    await store.initialize()
    receiver = FastAPI()
    receiver.include_router(router)
    receiver.state.store = store
    receiver.state.settings = SimpleNamespace(crop_dir=remote, photo_sync_devices_json=json.dumps({
        "device-bgd": {"site": "bgd", "token": bgd_token},
        "device-cas": {"site": "cas", "token": cas_token},
    }))
    _, encoded = cv2.imencode(".png", np.full((8, 8, 3), 120, dtype=np.uint8))
    png = encoded.tobytes()
    refs = {}
    try:
        for index, site in enumerate(("bgd", "cas"), start=1):
            context = warehouse_context.set(site)
            try:
                prefix = "cas/" if site == "cas" else ""
                top, label = f"{prefix}batch/top.png", f"{prefix}label-photos/qr.png"
                for ref, data in ((top, mark_final_png(png)), (label, png)):
                    path = local / ref
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                awb = await store.insert_awb(f"M{index}", hawb_number=f"H{index}")
                conn = store._connect()
                conn.execute("INSERT INTO parcel_identity (id,batch_id,track_id,status,model_name,model_version,crop_ref,awb_id,created_at) VALUES (?, 'batch', ?, 'NEW', 'm', 'v', ?, ?, 'now')", (index, index, top, awb.id))
                conn.execute("UPDATE awb SET status='PAIRED', parcel_id=?, label_photo_ref=? WHERE id=?", (index, label, awb.id))
                conn.commit()
                refs[site] = [top, label]
            finally:
                warehouse_context.reset(context)

        worker = PhotoSync(settings, store)
        await worker.reconcile()
        original_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.ASGITransport(receiver), **kwargs))
        task = asyncio.create_task(worker.run())
        try:
            async def finished():
                while any(set(worker.status(site, names).values()) != {"synced"} for site, names in refs.items()):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(finished(), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for names in refs.values():
            for ref in names:
                assert (remote / ref).read_bytes() == (local / ref).read_bytes()
    finally:
        await store.close()
