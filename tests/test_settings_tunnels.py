from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.security import require_operator
from app.settings_router import router
from app.warehouse import warehouse_context


@pytest.mark.asyncio
async def test_tunnel_controls_are_scoped_and_do_not_interrupt_scans(monkeypatch):
    import app.settings_router as module

    fake = MagicMock()
    fake.status.side_effect = lambda name: {"enabled": True, "state": "connected", "alive": True}
    monkeypatch.setattr(module, "manager", fake)
    app = FastAPI()
    app.state.settings = SimpleNamespace(jwt_secret="test-secret")
    app.include_router(router, prefix="/api")
    app.state.warehouse_runtimes = {"cas": SimpleNamespace(pipeline=SimpleNamespace(session=SimpleNamespace(running=True)))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/settings/tunnels")).status_code == 401
        app.dependency_overrides[require_operator] = lambda: None
        token = warehouse_context.set("cas")
        try:
            result = await client.get("/api/settings/tunnels")
            assert result.json()["warehouse"] == "cas"
            assert (await client.post("/api/settings/tunnels/manifest/stop")).status_code == 409
            fake.stop.assert_not_called()
            app.state.warehouse_runtimes["cas"].pipeline.session.running = False
            assert (await client.post("/api/settings/tunnels/manifest/restart")).status_code == 200
            fake.restart.assert_called_once_with("cas")
            assert (await client.post("/api/settings/tunnels/manifest/bgd")).status_code == 422
        finally:
            warehouse_context.reset(token)


@pytest.mark.asyncio
async def test_scan_start_and_tunnel_stop_cannot_race(monkeypatch):
    import app.settings_router as module
    from app.session_router import router as sessions

    fake = MagicMock()
    fake.status.return_value = {"enabled": True, "state": "connected", "alive": True}
    monkeypatch.setattr(module, "manager", fake)
    started, release = asyncio.Event(), asyncio.Event()
    pipeline = SimpleNamespace(session=SimpleNamespace(running=False))

    async def start():
        started.set()
        await release.wait()
        pipeline.session.running = True
        return "batch-1"

    pipeline.start_session = start
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(sessions, prefix="/api")
    app.state.settings = SimpleNamespace(jwt_secret="test-secret")
    app.state.warehouse_runtimes = {"cas": SimpleNamespace(pipeline=pipeline)}
    app.dependency_overrides[require_operator] = lambda: None
    token = warehouse_context.set("cas")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            starting = asyncio.create_task(client.post("/api/session/start"))
            await started.wait()
            stopping = asyncio.create_task(client.post("/api/settings/tunnels/manifest/stop"))
            await asyncio.sleep(0)
            release.set()
            assert (await starting).status_code == 200
            assert (await stopping).status_code == 409
            fake.stop.assert_not_called()
    finally:
        warehouse_context.reset(token)
import asyncio


@pytest.mark.parametrize("warehouse", ["bgd", "cas"])
async def test_sync_monitoring_only_reads_authenticated_warehouse(warehouse):
    from app.warehouse import current_warehouse

    app = FastAPI()
    app.state.settings = SimpleNamespace(jwt_secret="test-secret")
    app.include_router(router, prefix="/api")
    reads = []

    async def list_photo_references():
        reads.append(current_warehouse())
        return [f"{current_warehouse()}-photo.png"]

    sync = MagicMock(enabled=True)
    sync.status.return_value = {f"{warehouse}-photo.png": "synced"}
    app.state.photo_sync = sync
    app.state.store = SimpleNamespace(list_photo_references=list_photo_references)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/settings/sync")).status_code == 401
        app.dependency_overrides[require_operator] = lambda: None
        token = warehouse_context.set(warehouse)
        try:
            response = await client.get("/api/settings/sync")
        finally:
            warehouse_context.reset(token)

    assert response.json() == {
        "enabled": True,
        "items": {warehouse: {"synced": 1, "pending": 0, "failed": 0, "unknown": 0, "total": 1}},
    }
    assert reads == [warehouse]
    sync.status.assert_called_once_with(warehouse, [f"{warehouse}-photo.png"])
