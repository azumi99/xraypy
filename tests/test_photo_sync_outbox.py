import asyncio
import hashlib
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.photo_sync import PhotoSync


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "crops"
    (root / "batch").mkdir(parents=True)
    (root / "cas" / "batch").mkdir(parents=True)
    return SimpleNamespace(photo_sync_enabled=True, crop_dir=root,
                           photo_sync_outbox_path=tmp_path / "outbox.sqlite",
                           photo_sync_url="https://example.test", photo_sync_bgd_token="b" * 40,
                           photo_sync_cas_token="c" * 40)


async def run_until(sync, predicate):
    task = asyncio.create_task(sync.run())
    try:
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail("worker did not reach expected state")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_offline_retry_and_restart(settings, monkeypatch):
    ref = "cas/batch/x.png"
    data = b"immutable photo"
    (settings.crop_dir / ref).write_bytes(data)
    sync = PhotoSync(settings)
    sync.enqueue("cas", ref)
    sync.enqueue("cas", ref)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("offline", request=request)
        assert request.url == "https://example.test/api/sync/photos"
        assert request.headers["Authorization"] == "Bearer " + "c" * 40
        assert data in request.content
        assert hashlib.sha256(data).hexdigest().encode() in request.content
        assert ref.encode() in request.content
        return httpx.Response(200, json={"status": "created", "sha256": hashlib.sha256(data).hexdigest()})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    asyncio.run(run_until(sync, lambda: len(requests) == 1))
    assert sync.status("cas", [ref]) == {ref: "pending"}
    with sqlite3.connect(settings.photo_sync_outbox_path) as db:
        assert db.execute("SELECT attempts FROM photo_outbox").fetchone() == (1,)
        db.execute("UPDATE photo_outbox SET next_attempt=0")
    restarted = PhotoSync(settings)
    asyncio.run(run_until(restarted, lambda: restarted.status("cas", [ref]).get(ref) == "synced"))
    restarted.enqueue("cas", ref)
    assert restarted.status("cas", [ref, "cas/batch/unknown.png"]) == {ref: "synced", "cas/batch/unknown.png": "unknown"}
    assert len(requests) == 2


@pytest.mark.parametrize("code,state", [(200, "synced"), (404, "pending"), (409, "failed"), (403, "pending"), (503, "pending")])
def test_response_classification(settings, monkeypatch, code, state):
    ref = "batch/x.png"
    (settings.crop_dir / ref).write_bytes(b"photo")
    sync = PhotoSync(settings)
    sync.enqueue("bgd", ref)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(code, json={"status": "created", "sha256": hashlib.sha256(b"photo").hexdigest()})), **kwargs))
    def attempted():
        with sqlite3.connect(settings.photo_sync_outbox_path) as db:
            return db.execute("SELECT attempts FROM photo_outbox").fetchone()[0] > 0
    asyncio.run(run_until(sync, attempted))
    assert sync.status("bgd", [ref]) == {ref: state}


def test_tenancy_symlinks_and_mutation(settings, tmp_path, monkeypatch):
    ref = "batch/x.png"
    photo = settings.crop_dir / ref
    photo.write_bytes(b"original")
    sync = PhotoSync(settings)
    for site, bad in [("cas", ref), ("bgd", "cas/batch/x.png"), ("bgd", "../x"),
                      ("bgd", "batch/../x"), ("other", ref)]:
        with pytest.raises(ValueError):
            sync.enqueue(site, bad)
    (settings.crop_dir / "batch" / "link.png").symlink_to(photo)
    (settings.crop_dir / "link").symlink_to(settings.crop_dir / "batch", target_is_directory=True)
    for bad in ("batch/link.png", "link/x.png"):
        with pytest.raises(OSError):
            sync.enqueue("bgd", bad)
    sync.enqueue("bgd", ref)
    photo.write_bytes(b"changed")
    with pytest.raises(ValueError):
        sync.enqueue("bgd", ref)
    original = httpx.AsyncClient
    def unexpected(request):
        pytest.fail("changed photo was sent")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(unexpected), **kwargs))
    asyncio.run(run_until(sync, lambda: sync.status("bgd", [ref]).get(ref) == "failed"))


def test_disabled_and_insecure_url(settings):
    settings.photo_sync_enabled = False
    sync = PhotoSync(settings)
    sync.enqueue("bgd", "batch/x.png")
    assert sync.status("bgd", ["batch/x.png"]) == {}
    assert not settings.photo_sync_outbox_path.exists()
    settings.photo_sync_enabled = True
    settings.photo_sync_url = "http://example.test"
    with pytest.raises(ValueError):
        PhotoSync(settings)


def test_worker_sends_final_photo_to_tenant_scoped_receiver(settings, tmp_path, monkeypatch):
    import io

    from fastapi import FastAPI
    from PIL import Image

    from app.photo_receiver import router
    from app.warehouse import current_warehouse

    receiver = FastAPI()
    receiver.include_router(router)
    remote = tmp_path / "remote"
    remote.mkdir()
    import json as _json
    receiver.state.settings = SimpleNamespace(crop_dir=remote, photo_sync_devices_json=_json.dumps({"cas": {"site": "cas", "token": "c" * 40}}))

    class Store:
        async def photo_reference_exists(self, ref):
            return (current_warehouse(), ref) == ("cas", "cas/batch/x.png")

    receiver.state.store = Store()
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(image, "PNG")
    photo = image.getvalue()
    (settings.crop_dir / "cas/batch/x.png").write_bytes(photo)
    sync = PhotoSync(settings)
    sync.enqueue("cas", "cas/batch/x.png")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.ASGITransport(receiver), **kwargs))
    asyncio.run(run_until(sync, lambda: sync.status("cas", ["cas/batch/x.png"]).get("cas/batch/x.png") == "synced"))
    assert (remote / "cas/batch/x.png").read_bytes() == photo
    assert PhotoSync(settings).status("cas", ["cas/batch/x.png"]) == {"cas/batch/x.png": "synced"}


def test_plain_http_200_without_verified_digest_stays_pending(settings, monkeypatch):
    ref = "batch/x.png"
    (settings.crop_dir / ref).write_bytes(b"photo")
    sync = PhotoSync(settings)
    sync.enqueue("bgd", ref)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="login page")), **kwargs))

    def attempted():
        with sqlite3.connect(settings.photo_sync_outbox_path) as db:
            return db.execute("SELECT attempts FROM photo_outbox").fetchone()[0] > 0

    asyncio.run(run_until(sync, attempted))
    assert sync.status("bgd", [ref]) == {ref: "pending"}


def test_concurrent_workers_claim_only_one_upload(settings, monkeypatch):
    ref = "batch/x.png"
    (settings.crop_dir / ref).write_bytes(b"photo")
    first = PhotoSync(settings)
    second = PhotoSync(settings)
    first.enqueue("bgd", ref)
    calls = []
    original = httpx.AsyncClient

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"status": "created", "sha256": hashlib.sha256(b"photo").hexdigest()})

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
        tasks = [asyncio.create_task(first.run()), asyncio.create_task(second.run())]
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(0.1)
            assert len(calls) == 1
            release.set()
            for _ in range(100):
                if first.status("bgd", [ref]).get(ref) == "synced":
                    break
                await asyncio.sleep(0.01)
            assert first.status("bgd", [ref]) == {ref: "synced"}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_reconcile_queues_only_finalized_referenced_photos(settings):
    import cv2
    import numpy as np

    from app.photo_sync import is_final_png, mark_final_png
    from app.warehouse import current_warehouse

    _, image = cv2.imencode(".png", np.zeros((8, 8, 3), dtype=np.uint8))
    original = image.tobytes()
    final = mark_final_png(original)
    assert is_final_png(final) and not is_final_png(original)
    assert np.array_equal(cv2.imdecode(np.frombuffer(final, np.uint8), 1), cv2.imdecode(np.frombuffer(original, np.uint8), 1))
    (settings.crop_dir / "batch/final.png").write_bytes(final)
    (settings.crop_dir / "batch/not-final.png").write_bytes(original)
    (settings.crop_dir / "cas/batch/other.png").write_bytes(final)
    (settings.crop_dir / "label-photos").mkdir()
    (settings.crop_dir / "label-photos/abc.jpg").write_bytes(b"label")

    class Store:
        async def list_photo_references(self):
            if current_warehouse() == "bgd":
                return ["batch/final.png", "batch/not-final.png", "label-photos/abc.jpg"]
            return ["cas/batch/other.png"]

    sync = PhotoSync(settings, Store())
    asyncio.run(sync.reconcile())
    assert sync.status("bgd", ["batch/final.png", "batch/not-final.png", "label-photos/abc.jpg"]) == {
        "batch/final.png": "pending", "batch/not-final.png": "failed", "label-photos/abc.jpg": "pending",
    }
    assert sync.status("cas", ["cas/batch/other.png"]) == {"cas/batch/other.png": "pending"}
