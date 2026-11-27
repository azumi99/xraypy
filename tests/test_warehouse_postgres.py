"""Integration test; only run against an explicitly disposable database."""

import os

import asyncpg
import cv2
import numpy as np
import pytest

from app.db import MIGRATIONS_DIR, _active_dim, apply_migrations
from app.dedup.pg_store import PgVectorStore
from app.warehouse import warehouse_context


@pytest.mark.asyncio
async def test_postgres_warehouse_migration_and_isolation(settings):
    dsn = os.environ.get("CV_DISPOSABLE_TEST_DSN")
    if not dsn:
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS cv")
            await conn.execute("CREATE TABLE cv.schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
            for path in sorted(MIGRATIONS_DIR.glob("00[1-5]*.sql")):
                sql = path.read_text().replace("{embedding_dim}", str(_active_dim(settings)))
                sql = sql.replace("{hnsw_m}", str(settings.db.hnsw.m)).replace("{hnsw_ef}", str(settings.db.hnsw.ef_construction))
                await conn.execute(sql)
                await conn.execute("INSERT INTO cv.schema_migrations(name) VALUES($1)", path.name)
            await conn.execute("INSERT INTO cv.awb(awb_number) VALUES('LEGACY-AWB')")
            await conn.execute("INSERT INTO cv.label_print(mawb,hawb,part) VALUES('M1','H1',1)")
            for path in sorted(MIGRATIONS_DIR.glob("00[6-8]*.sql")):
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO cv.schema_migrations(name) VALUES($1)", path.name)
            for site in ("bgd", "cas"):
                await conn.execute(f"ALTER TABLE cv.awb_{site} ADD UNIQUE (hawb_number)")
                if site == "cas":
                    await conn.execute("INSERT INTO cv.awb_cas(awb_number, hawb_number) VALUES('LEGACY-AWB', 'HOUSE')")
                else:
                    await conn.execute("UPDATE cv.awb_bgd SET hawb_number='HOUSE'")
        await apply_migrations(pool, settings)
        await apply_migrations(pool, settings)  # Re-running migrations preserves nullable legacy data.
        store = PgVectorStore(settings)
        store._pool = pool
        token = warehouse_context.set("bgd")
        try:
            old = (await store.list_awbs())[0]
            assert old.awb_number == "LEGACY-AWB" and old.part is None and old.total is None
            assert len(await store.list_labels()) == 1
            assert await store.label_mawbs("H1") == ["M1"]
        finally:
            warehouse_context.reset(token)
        token = warehouse_context.set("cas")
        try:
            assert len(await store.list_awbs()) == 1
            assert await store.list_labels() == []
            new = await store.insert_awb("LEGACY-AWB", hawb_number="HOUSE", part=2, total=5)
            assert (new.part, new.total) == (2, 5)
            assert (await store.list_awbs())[-1].total == 5
            again = await store.insert_awb("LEGACY-AWB")
            assert new.id != again.id
            async with pool.acquire() as conn:
                await conn.execute("UPDATE cv.awb_cas SET hawb_number='HOUSE' WHERE id=$1", new.id)
            row = ("M1", 1, "H1", 1, 1, None, None, None)
            await store.import_labels([row], "PJT-A")
            assert (await store.import_labels([row], "PJT-B"))["imported"] == 1
            assert (await store.import_labels([row], "PJT-B"))["updated"] == 1
            assert (await store.import_labels([row]))["imported"] == 1
            assert (await store.import_labels([row]))["updated"] == 1
            assert len(await store.list_labels("PJT-A")) == 1
            assert len(await store.list_labels("PJT-B")) == 1
            assert len(await store.list_awbs()) == 3
            assert len(await store.list_labels()) == 3
            assert await store.label_mawbs("H1") == ["M1"]
        finally:
            warehouse_context.reset(token)
        token = warehouse_context.set("bgd")
        try:
            assert len(await store.list_awbs()) == 1
            assert (await store.list_awbs())[0].part is None
            new = await store.insert_awb("LEGACY-AWB")
            async with pool.acquire() as conn:
                await conn.execute("UPDATE cv.awb_bgd SET hawb_number='HOUSE' WHERE id=$1", new.id)
            assert len(await store.list_labels()) == 1
        finally:
            warehouse_context.reset(token)
    finally:
        await pool.close()


def test_postgres_login_and_cross_warehouse_awb_access(tmp_path, monkeypatch):
    dsn = os.environ.get("CV_DISPOSABLE_TEST_DSN")
    if not dsn:
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    from fastapi.testclient import TestClient
    from tests.conftest import StubEmbedder
    import app.main as main
    from app.config import Settings

    monkeypatch.setattr(main, "create_embedder", lambda _: StubEmbedder())
    settings = Settings(_env_file=None, database_url=dsn, jwt_secret="a" * 32,
                        bootstrap_password="admin123", crop_dir=tmp_path / "crops")
    with TestClient(main.create_app(settings)) as client:
        assert client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"}).status_code == 200
        bgd = client.post("/api/awb", json={"awb_number": "BGD-INTEGRATION"}).json()
        client.post("/api/auth/logout")
        assert client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get(f"/api/awb/{bgd['id']}").status_code == 404
        assert all(a["awb_number"] != "BGD-INTEGRATION" for a in client.get("/api/awb").json()["items"])
        cas = client.post("/api/awb", json={"awb_number": "CAS-INTEGRATION"}).json()
        client.post("/api/auth/logout")
        assert client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get(f"/api/awb/{cas['id']}").status_code == 404


@pytest.mark.asyncio
async def test_postgres_pairing_isolated(settings, shape):
    dsn = os.environ.get("CV_DISPOSABLE_TEST_DSN")
    if not dsn:
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = PgVectorStore(settings)
    store._pool = pool
    embedding = np.ones(_active_dim(settings), dtype=np.float32)
    embedding /= np.linalg.norm(embedding)
    records = {}
    try:
        for access in ("bgd", "cas"):
            token = warehouse_context.set(access)
            try:
                own_awbs = {row.id for row in await store.list_awbs()}
                record, paired = await store.finalize_new(
                    batch_id="integration-pair", track_id=1, embedding=embedding,
                    model_name="test", model_version="1", shape=shape,
                    crop_ref=None, sim_score=None, require_awb=True,
                )
                assert paired is not None and paired.id in own_awbs
                records[access] = record
            finally:
                warehouse_context.reset(token)
        assert records["bgd"].id != records["cas"].id
        for access, other in (("bgd", "cas"), ("cas", "bgd")):
            token = warehouse_context.set(access)
            try:
                assert await store.get(records[other].id) is None
                matches = await store.find_similar(embedding, model_name="test", model_version="1", batch_id="integration-pair")
                assert all(match.record.id != records[other].id for match in matches)
            finally:
                warehouse_context.reset(token)
    finally:
        await pool.close()


def test_postgres_qr_photo_and_timestamp_isolated(tmp_path, monkeypatch):
    dsn = os.environ.get("CV_DISPOSABLE_TEST_DSN")
    if not dsn:
        pytest.skip("requires an explicitly disposable PostgreSQL database")
    from fastapi.testclient import TestClient
    from tests.conftest import StubEmbedder
    import app.main as main
    from app.config import Settings

    monkeypatch.setattr(main, "create_embedder", lambda _: StubEmbedder())
    settings = Settings(_env_file=None, database_url=dsn, jwt_secret="a" * 32,
                        bootstrap_password="admin123", crop_dir=tmp_path / "crops")
    _, encoded = cv2.imencode(".jpg", np.zeros((10, 10, 3), dtype=np.uint8))
    with TestClient(main.create_app(settings)) as client:
        refs = {}
        for site in ("bgd", "cas"):
            assert client.post("/api/auth/login", json={"email": f"{site}@paratekno.com", "password": "admin123"}).status_code == 200
            result = client.post("/api/awb", json={"qr_code": "H1_1_1"})
            assert result.status_code == 200, result.text
            awb = result.json()
            assert awb["timecreated"] is not None
            photo = client.post(f"/api/awb/{awb['id']}/label-photo", files={"file": ("label.jpg", encoded.tobytes(), "image/jpeg")})
            assert photo.status_code == 200, photo.text
            assert photo.json()["timecreated"] == awb["timecreated"]
            refs[site] = photo.json()["label_photo_ref"]
            assert client.get(f"/crops/{refs[site]}").status_code == 200
            if site == "cas":
                assert client.get(f"/crops/{refs['bgd']}").status_code == 403
            client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"})
        assert client.get(f"/crops/{refs['cas']}").status_code == 403
