from unittest.mock import AsyncMock, MagicMock

import pytest

from app import db, db_tunnel
from app.config import Settings


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv("CV_DATABASE_URL", raising=False)
    return ({"CV_DB_TUNNEL_ENABLED": "true", "CV_DB_TUNNEL_SSH_HOST": "ssh.example.test",
             "CV_DB_TUNNEL_SSH_USER": "tunnel"},
            Settings(_env_file=None, database_url="postgresql://user:secret@127.0.0.1:15432/db"))


@pytest.mark.parametrize("key,value", [
    ("CV_DB_TUNNEL_ENABLED", "yes"), ("CV_DB_TUNNEL_SSH_HOST", "-bad"),
    ("CV_DB_TUNNEL_SSH_USER", "bad user"), ("CV_DB_TUNNEL_SSH_PORT", "65536"),
    ("CV_DB_TUNNEL_SSH_KEY", "relative/key"),
])
def test_invalid_config(config, key, value):
    env, settings = config
    env[key] = value
    with pytest.raises(ValueError):
        db_tunnel._config(env, settings)


@pytest.mark.parametrize("dsn", ["postgresql://localhost:15432/db", "postgresql://127.0.0.1:5432/db",
    "postgresql://127.0.0.1:15432/db?host=evil", "host=127.0.0.1 port=15432",
    "postgresql://127.0.0.1:15432/db?port=5432"])
def test_wrong_dsn(config, dsn):
    env, settings = config
    settings.database_url = dsn
    with pytest.raises(ValueError):
        db_tunnel._config(env, settings)


async def test_pool_rejects_dead_or_replaced_tunnel(config, monkeypatch):
    _, settings = config
    process = MagicMock(returncode=None)
    entry = {"process": process}
    token = db.tunnel_entry.set(entry)
    captured = {}

    async def create_pool(*args, **kwargs):
        captured["connect"] = kwargs["connect"]
        raise OSError("no database access")

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    try:
        with pytest.raises(db.DatabaseUnavailableError):
            await db.create_pool(settings)
    finally:
        db.tunnel_entry.reset(token)
    connect = captured["connect"]
    process.returncode = 1
    with pytest.raises(db.DatabaseUnavailableError):
        await connect()
    process.returncode = None
    connection = MagicMock(close=AsyncMock())

    async def replace(*args, **kwargs):
        entry["process"] = MagicMock(returncode=None)
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", replace)
    with pytest.raises(db.DatabaseUnavailableError):
        await connect()
    connection.close.assert_awaited_once()
