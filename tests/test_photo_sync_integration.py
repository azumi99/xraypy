import hashlib
import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.config import DedupConfig, Settings
from tests.conftest import StubEmbedder


def test_receiver_middleware_and_warehouse_isolation(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "create_embedder", lambda _: StubEmbedder())
    settings = Settings(_env_file=None, jwt_secret="x" * 32, bootstrap_password="admin123",
                        sqlite_path=str(tmp_path / "db.sqlite"), crop_dir=tmp_path / "crops",
                        dedup=DedupConfig(store="sqlite"), photo_sync_devices_json=json.dumps({"bgd": {"site": "bgd", "token": "b" * 40}, "cas": {"site": "cas", "token": "c" * 40}}))
    _, encoded = cv2.imencode(".png", np.zeros((10, 10, 3), dtype=np.uint8))
    data = encoded.tobytes()
    digest = hashlib.sha256(data).hexdigest()
    with TestClient(main.create_app(settings)) as client:
        assert client.put("/api/sync/photos", content=b"x").status_code == 401
        assert client.put("/api/sync/photos", headers={"Authorization": "Bearer " + "b" * 40,
                                                       "Content-Length": "999999999"}, content=b"x").status_code == 413
        assert client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"}).status_code == 200
        awb = client.post("/api/awb", json={"awb_number": "M123"}).json()
        attached = client.post(f"/api/awb/{awb['id']}/label-photo", files={"file": ("a.png", data, "image/png")})
        assert attached.status_code == 200
        ref = attached.json()["label_photo_ref"]
        assert client.get("/api/sync/status", params={"ref": ref}).json() == {"items": {}}
        client.post("/api/auth/logout")
        upload = lambda token: client.put("/api/sync/photos", headers={"Authorization": "Bearer " + token},
                                          data={"ref": ref, "sha256": digest},
                                          files={"file": ("a.png", data, "image/png")})
        assert upload("c" * 40).status_code == 400
        assert upload("b" * 40).json() == {"status": "exists", "sha256": digest}
        downloaded = client.get("/api/sync/photos", params={"ref": ref}, headers={"Authorization": "Bearer " + "b" * 40})
        assert downloaded.status_code == 200 and downloaded.content == data
        assert downloaded.headers["x-content-sha256"] == digest
        assert client.get("/api/sync/photos", params={"ref": ref}, headers={"Authorization": "Bearer " + "c" * 40}).status_code == 404
        assert client.post("/api/sync/photos").status_code == 401
        assert client.get("/api/awb").status_code == 401
