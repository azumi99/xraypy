import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.photo_sync import PhotoSyncService
from app.photos import router
from app.security import require_operator
from app.warehouse import warehouse_context


@pytest.mark.parametrize("warehouse", ["bgd", "cas"])
@pytest.mark.parametrize("path", ["batch/x.png", "cas/b/x.jpg", "../outside.png", "link/x.png"])
def test_sync_disabled_without_modifying_files(tmp_path, warehouse, path):
    root = tmp_path / "crops"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"existing")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_operator] = lambda: None
    app.state.settings = Settings(_env_file=None, crop_dir=root)
    token = warehouse_context.set(warehouse)
    try:
        with TestClient(app) as client:
            response = client.post("/api/photos/sync", data={"path": path},
                                   files={"file": ("x.png", b"attacker", "image/png")})
    finally:
        warehouse_context.reset(token)
    assert response.status_code == 503
    assert outside.read_bytes() == b"existing"
    assert sorted(p.name for p in root.iterdir()) == ["link"]


def test_sync_requires_authentication():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.settings = Settings(_env_file=None)
    with TestClient(app) as client:
        assert client.post("/api/photos/sync").status_code == 401


@pytest.mark.parametrize("url", ["", "http://example.com", "https://example.com"])
def test_sync_cannot_be_started_even_if_enabled(url):
    settings = Settings(_env_file=None, photo_sync={"enabled": True, "base_url": url})
    with pytest.raises(RuntimeError, match="disabled"):
        PhotoSyncService(settings)
