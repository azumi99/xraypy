import asyncio
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.photo_receiver import MAX_BYTES, router
from app.warehouse import warehouse_context

devices = {"bgd": {"site": "bgd", "token": "b" * 40}, "cas": {"site": "cas", "token": "c" * 40}}


def image(color):
    data = io.BytesIO()
    Image.new("RGB", (3, 3), color).save(data, format="PNG")
    return data.getvalue()


class Store:
    def __init__(self):
        self.refs = {"bgd": {"batch/a.png"}, "cas": {"cas/batch/a.png"}}
        self.calls = []

    async def photo_reference_exists(self, ref):
        site = warehouse_context.get()
        self.calls.append((site, ref))
        return ref in self.refs[site]


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(crop_dir=tmp_path, photo_sync_devices_json=json.dumps(devices))
    app.state.store = Store()
    with TestClient(app) as client:
        yield app, client, tmp_path


def upload(client, ref="batch/a.png", content=None, token="b" * 40, digest=None):
    if content is None:
        content = image("red")
    return client.put("/api/sync/photos", headers={"Authorization": f"Bearer {token}"},
                      data={"ref": ref, "sha256": digest or hashlib.sha256(content).hexdigest()},
                      files={"file": ("photo.png", content, "image/png")})


def test_auth_and_disabled(setup):
    app, client, _ = setup
    assert upload(client, token="wrong").status_code == 401
    assert client.put("/api/sync/photos", data={"ref": "x", "sha256": "x"},
                      files={"file": ("x", b"x")}).status_code == 401
    app.state.settings.photo_sync_devices_json = ""
    assert upload(client).status_code == 503
    assert not app.state.store.calls


def test_device_scoped_tokens_can_be_revoked_individually(setup):
    import json

    app, client, _ = setup
    devices = {"device-a": {"site": "bgd", "token": "a" * 40}, "device-b": {"site": "bgd", "token": "b" * 40}}
    app.state.settings.photo_sync_devices_json = json.dumps(devices)
    assert upload(client, token="a" * 40).status_code == 200
    del devices["device-a"]
    app.state.settings.photo_sync_devices_json = json.dumps(devices)
    assert upload(client, token="a" * 40).status_code == 401
    assert upload(client, token="b" * 40).status_code == 200


async def test_slow_photo_publication_does_not_block_other_requests(tmp_path, monkeypatch):
    from app import photo_receiver

    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(crop_dir=tmp_path, photo_sync_devices_json=json.dumps(devices))
    app.state.store = Store()
    entered, release = threading.Event(), threading.Event()

    def slow_publish(*args):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test did not release publication")
        return True

    monkeypatch.setattr(photo_receiver, "_validate_and_publish", slow_publish)

    @app.get("/health")
    async def health():
        return {"ok": True}

    data = image("red")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        task = asyncio.create_task(client.put(
            "/api/sync/photos", headers={"Authorization": "Bearer " + "b" * 40},
            data={"ref": "batch/a.png", "sha256": hashlib.sha256(data).hexdigest()},
            files={"file": ("a.png", data, "image/png")},
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await asyncio.wait_for(client.get("/health"), 0.5)).json() == {"ok": True}
        finally:
            release.set()
        assert (await task).status_code == 200


@pytest.mark.parametrize("ref,token", [
    ("cas/batch/a.png", "b" * 40), ("CAS/batch/a.png", "b" * 40), ("batch/a.png", "c" * 40),
    ("../a.png", "b" * 40), ("/a.png", "b" * 40),
    ("a/../b.png", "b" * 40), ("a\\b.png", "b" * 40),
    ("a//b.png", "b" * 40),
])
def test_invalid_paths(setup, ref, token):
    app, client, _ = setup
    assert upload(client, ref=ref, token=token).status_code == 400
    assert not app.state.store.calls


def test_reference_digest_image_and_size(setup):
    app, client, root = setup
    assert upload(client, ref="unknown.png").status_code == 404
    assert upload(client, digest="0" * 64).status_code == 400
    assert upload(client, content=b"not an image").status_code == 400
    assert upload(client, content=b"x" * (MAX_BYTES + 1)).status_code == 413
    assert not list(root.iterdir())
    assert app.state.store.calls[0] == ("bgd", "unknown.png")


def test_idempotency_and_tenant_context(setup):
    app, client, root = setup
    assert upload(client).json() == {"status": "created", "sha256": hashlib.sha256(image("red")).hexdigest()}
    assert upload(client).json() == {"status": "exists", "sha256": hashlib.sha256(image("red")).hexdigest()}
    assert upload(client, content=image("blue")).status_code == 409
    assert (root / "batch/a.png").read_bytes() == image("red")
    assert upload(client, ref="cas/batch/a.png", token="c" * 40).status_code == 200
    assert app.state.store.calls[-1] == ("cas", "cas/batch/a.png")
    assert warehouse_context.get() == "bgd"


def test_symlinks(setup, tmp_path):
    app, client, root = setup
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (root / "batch").symlink_to(outside, target_is_directory=True)
    assert upload(client).status_code == 400
    assert not list(outside.iterdir())
    (root / "batch").unlink()
    (root / "batch").mkdir()
    (root / "batch/a.png").symlink_to(outside / "target")
    assert upload(client).status_code == 400
    assert not list(outside.iterdir())


def test_concurrent_different_content(setup):
    _, client, root = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda color: upload(client, content=image(color)), ["red", "blue"]))
    assert sorted(response.status_code for response in results) == [200, 409]
    assert (root / "batch/a.png").read_bytes() in (image("red"), image("blue"))
    assert sorted(p.name for p in (root / "batch").iterdir()) == ["a.png"]
