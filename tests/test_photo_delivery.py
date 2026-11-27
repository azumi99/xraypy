import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.photo_delivery import router
from app.photo_receiver import router as receiver
from app.security import issue_session
from tests.test_photo_receiver import Store, image


@pytest.mark.parametrize("windows", [False, True])
def test_obs_delivery_uses_only_obs_even_with_a_local_copy(tmp_path, monkeypatch, windows):
    monkeypatch.setattr("app.photo_delivery._WINDOWS", windows)
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(crop_dir=tmp_path, jwt_secret="s" * 32)
    app.state.store = Store()
    remote_image = image("blue")
    objects = {"bgd/crops/batch/a.png": remote_image}

    def read(key):
        if key not in objects:
            raise FileNotFoundError(key)
        return objects[key]

    app.state.photo_sync = SimpleNamespace(enabled=False, obs=SimpleNamespace(read=read))
    (tmp_path / "batch").mkdir()
    (tmp_path / "batch/a.png").write_bytes(image("red"))
    jwt, _ = issue_session(app.state.settings, {"email": "x", "access": "bgd"})
    with TestClient(app) as client:
        client.cookies.set("cv_session", jwt)
        url = "/api/sync/photo?ref=batch/a.png"
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == remote_image
        objects.clear()
        assert client.get(url).status_code == 404


def test_delivery_and_receiver(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.include_router(receiver)
    devices = {"bgd": {"site": "bgd", "token": "b" * 40}, "cas": {"site": "cas", "token": "c" * 40}}
    app.state.settings = SimpleNamespace(crop_dir=tmp_path, jwt_secret="s" * 32,
                                         photo_sync_devices_json=json.dumps(devices))
    app.state.store = Store()
    app.state.photo_sync = SimpleNamespace(enabled=True, url="https://vm.example/api/sync/photos",
                                           tokens={"bgd": "b" * 40, "cas": "c" * 40})
    content = image("red")
    calls = []

    def remote(request):
        calls.append(request)
        return httpx.Response(200, content=content,
                              headers={"X-Content-SHA256": hashlib.sha256(content).hexdigest()})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.photo_delivery.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(remote), **kwargs))
    with TestClient(app) as client:
        assert client.get("/api/sync/photo", params={"ref": "batch/a.png"}).status_code == 401
        jwt, _ = issue_session(app.state.settings, {"email": "x", "access": "bgd"})
        client.cookies.set("cv_session", jwt)
        result = client.get("/api/sync/photo", params={"ref": "batch/a.png"})
        assert result.status_code == 200 and result.content == content
        assert calls[0].headers["authorization"] == "Bearer " + "b" * 40
        assert calls[0].url.params["ref"] == "batch/a.png"
        (tmp_path / "batch").mkdir()
        (tmp_path / "batch/a.png").write_bytes(content)
        assert client.get("/api/sync/photo", params={"ref": "batch/a.png"}).content == content
        assert len(calls) == 1
        assert client.get("/api/sync/photo", params={"ref": "cas/batch/a.png"}).status_code == 404
        assert client.get("/api/sync/photo", params={"ref": "batch/unknown.png"}).status_code == 404
        machine = client.get("/api/sync/photos", params={"ref": "batch/a.png"},
                             headers={"Authorization": "Bearer " + "b" * 40})
        assert machine.content == content and machine.headers["content-type"] == "image/png"
        assert machine.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
        assert client.get("/api/sync/photos", params={"ref": "batch/a.png"},
                          headers={"Authorization": "Bearer " + "c" * 40}).status_code == 404
        (tmp_path / "batch/a.png").unlink()
        jwt, _ = issue_session(app.state.settings, {"email": "x", "access": "cas"})
        client.cookies.set("cv_session", jwt)
        assert client.get("/api/sync/photo", params={"ref": "cas/batch/a.png"}).content == content
        assert calls[-1].headers["authorization"] == "Bearer " + "c" * 40
        assert client.get("/api/sync/photo", params={"ref": "batch/a.png"}).status_code == 404
        jwt, _ = issue_session(app.state.settings, {"email": "x", "access": "bgd"})
        client.cookies.set("cv_session", jwt)
        app.state.photo_sync.enabled = False
        assert client.get("/api/sync/photo", params={"ref": "batch/a.png"}).status_code == 404


def test_remote_rejects_redirect_and_bad_digest(tmp_path, monkeypatch):
    from app.photo_delivery import _remote
    import asyncio
    import pytest
    from fastapi import HTTPException

    sync = SimpleNamespace(url="https://vm.example/api/sync/photos", tokens={"bgd": "secret"})
    real_client = httpx.AsyncClient
    for response in (httpx.Response(302, headers={"Location": "https://evil.example"}),
                     httpx.Response(200, content=image("red"), headers={"X-Content-SHA256": "0" * 64})):
        monkeypatch.setattr("app.photo_delivery.httpx.AsyncClient", lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: response), **kwargs))
        with pytest.raises(HTTPException) as error:
            asyncio.run(_remote(sync, "bgd", "batch/a.png"))
        assert error.value.status_code == 503
