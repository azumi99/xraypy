from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import db
from app.config import Settings


async def test_pgvector_codec_initialized_once_per_new_pool_connection(monkeypatch):
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=1))

    @asynccontextmanager
    async def acquire():
        yield connection

    pool = SimpleNamespace(acquire=acquire, expire_connections=AsyncMock(), close=AsyncMock())
    callback = None
    codec = AsyncMock()

    async def create_pool(*args, **kwargs):
        nonlocal callback
        callback = kwargs["init"]
        await callback(connection)  # bootstrap before extension exists
        return pool

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(db, "register_vector", codec)
    migrations = AsyncMock()
    monkeypatch.setattr(db, "apply_migrations", migrations)
    assert await db.create_pool(Settings(_env_file=None, database_url="postgresql://test")) is pool
    codec.assert_not_awaited()
    migrations.assert_awaited_once()
    pool.expire_connections.assert_awaited_once()
    await callback(connection)  # newly established connection after migration
    codec.assert_awaited_once_with(connection)
