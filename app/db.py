"""Database bootstrap: DSN resolution, migration runner, asyncpg pool."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

from .config import Settings

logger = logging.getLogger(__name__)
tunnel_entry: ContextVar[dict | None] = ContextVar("db_tunnel_entry", default=None)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class DatabaseUnavailableError(RuntimeError):
    """Raised at startup when Postgres/pgvector is unreachable or broken."""


def resolve_dsn(settings: Settings) -> str:
    dsn_env = settings.db.dsn_env
    import os

    dsn = os.environ.get(dsn_env, "") or settings.database_url
    if not dsn:
        raise DatabaseUnavailableError(
            f"No database DSN: set {dsn_env} (or CV_DATABASE_URL). "
            "Example: postgresql://user@localhost:5432/object_cv"
        )
    return dsn


async def apply_migrations(pool: asyncpg.Pool, settings: Settings) -> None:
    """Apply SQL migrations in filename order; tracked in cv.schema_migrations."""
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS cv")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS cv.schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {r["name"] for r in await conn.fetch("SELECT name FROM cv.schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            sql = path.read_text()
            sql = sql.replace("{embedding_dim}", str(_active_dim(settings)))
            sql = sql.replace("{hnsw_m}", str(settings.db.hnsw.m))
            sql = sql.replace("{hnsw_ef}", str(settings.db.hnsw.ef_construction))
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO cv.schema_migrations(name) VALUES ($1)", path.name)
            logger.info("applied migration %s", path.name)


def _active_dim(settings: Settings) -> int:
    from .embedding.registry import model_dim

    return model_dim(settings.embedding.model, settings.embedding.pooling)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    dsn = resolve_dsn(settings)
    entry = tunnel_entry.get()
    vector_ready = False

    async def init_connection(connection):
        if vector_ready:
            await register_vector(connection)

    async def connect(*args, **kwargs):
        process = entry["process"]
        if process is None or process.returncode is not None:
            raise DatabaseUnavailableError("Database tunnel is unavailable")
        connection = await asyncpg.connect(*args, **kwargs)
        if entry["process"] is not process or process.returncode is not None:
            await connection.close()
            raise DatabaseUnavailableError("Database tunnel disconnected during connection")
        return connection

    try:
        options = {"connect": connect} if entry is not None else {}
        options["init"] = init_connection
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, **options)
    except OSError as exc:  # connection refused etc.
        raise DatabaseUnavailableError(
            f"Cannot reach PostgreSQL ({exc}). Is it running? Install pgvector: `brew install pgvector`."
        ) from exc
    try:
        async with pool.acquire() as conn:
            has_vector = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_available_extensions WHERE name='vector'"
            )
            if not has_vector:
                raise DatabaseUnavailableError(
                    "pgvector extension not available on this server. "
                    "Install it (`brew install pgvector`) and restart Postgres."
                )
        await apply_migrations(pool, settings)
        vector_ready = True
        # Bootstrap connections were opened before CREATE EXTENSION; replace
        # them so every physical connection receives the codec once on creation.
        await pool.expire_connections()
    except asyncpg.PostgresError as exc:  # pragma: no cover - defensive
        await pool.close()
        raise DatabaseUnavailableError(f"Database error during startup: {exc}") from exc
    return pool
