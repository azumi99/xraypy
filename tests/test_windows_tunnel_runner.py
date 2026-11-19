from contextlib import asynccontextmanager

import pytest

from tools import db_tunnel_win


@pytest.mark.asyncio
async def test_runner_uses_shared_configured_tunnel_lifecycle(monkeypatch):
    events = []
    settings = object()

    @asynccontextmanager
    async def manifest():
        events.append("manifest-start")
        yield
        events.append("manifest-stop")

    @asynccontextmanager
    async def database(value):
        assert value is settings
        events.append("database-start")
        yield
        events.append("database-stop")

    class StopImmediately:
        async def wait(self):
            events.append("wait")

    monkeypatch.setattr(db_tunnel_win, "manifest_tunnels", manifest)
    monkeypatch.setattr(db_tunnel_win, "db_tunnel", database)
    monkeypatch.setattr(db_tunnel_win, "load_settings", lambda: settings)
    monkeypatch.setattr(db_tunnel_win.asyncio, "Event", StopImmediately)
    await db_tunnel_win.main()
    assert events == ["manifest-start", "database-start", "wait", "database-stop", "manifest-stop"]
