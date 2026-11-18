import asyncio
import sqlite3

import numpy as np
import pytest

from app.dedup.sqlite_store import SqliteStore
from app.warehouse import current_warehouse, warehouse_context, warehouse_sql


async def test_warehouse_identity_pairing_and_labels(store, shape):
    embedding = np.ones(64, dtype=np.float32)
    label = ("MAWB", 1, "HAWB", 1, 1, "air", "flight", "route")

    async def create(warehouse):
        token = warehouse_context.set(warehouse)
        try:
            awb = await store.insert_awb("same", "batch")
            await store.import_labels([label])
            identity, paired = await store.finalize_new(
                batch_id="batch", track_id=1, embedding=embedding, model_name="m",
                model_version="v", shape=shape, crop_ref=None, sim_score=None,
                require_awb=True,
            )
            return identity, awb, paired
        finally:
            warehouse_context.reset(token)

    bgd, cas = await asyncio.gather(create("bgd"), create("cas"))
    assert bgd[0].id != cas[0].id and bgd[1].id != cas[1].id
    assert bgd[2].id == bgd[1].id and cas[2].id == cas[1].id
    for warehouse, own, other in (("bgd", bgd, cas), ("cas", cas, bgd)):
        token = warehouse_context.set(warehouse)
        try:
            assert await store.get(other[0].id) is None
            assert (await store.get_by_track("batch", 1)).id == own[0].id
            assert len(await store.list_labels()) == 1
            assert not await store.delete_awb(other[1].id, force=True)
            matches = await store.find_similar(embedding, model_name="m", model_version="v", batch_id="batch")
            assert [m.record.id for m in matches] == [own[0].id]
            await store.add_side_capture(other[0].id, embedding, "bad", model_name="m", model_version="v")
            await store.insert_audit(other[0].id, "bad", "x", None, None, None)
            assert await store.list_audit(other[0].id) == []
        finally:
            warehouse_context.reset(token)


async def test_legacy_sqlite_rows_migrate_to_bgd(settings):
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute("CREATE TABLE awb (id INTEGER PRIMARY KEY AUTOINCREMENT, awb_number TEXT UNIQUE NOT NULL, batch_id TEXT, status TEXT DEFAULT 'WAITING', parcel_id INTEGER, paired_at TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO awb (awb_number, created_at) VALUES ('legacy', '2026-01-01')")
    conn.commit()
    conn.close()
    store = SqliteStore(settings)
    await store.initialize()
    try:
        assert [row.awb_number for row in await store.list_awbs()] == ["legacy"]
        token = warehouse_context.set("cas")
        try:
            assert await store.list_awbs() == []
        finally:
            warehouse_context.reset(token)
    finally:
        await store.close()


async def test_duplicate_numbers_migration_and_pairing(settings, shape):
    with sqlite3.connect(settings.sqlite_path) as conn:
        for site in ("bgd", "cas"):
            conn.execute(f"CREATE TABLE awb_{site} (id INTEGER PRIMARY KEY AUTOINCREMENT, awb_number TEXT NOT NULL UNIQUE, hawb_number TEXT UNIQUE, batch_id TEXT, status TEXT NOT NULL DEFAULT 'WAITING', parcel_id INTEGER, paired_at TEXT, created_at TEXT NOT NULL)")
            conn.execute(f"INSERT INTO awb_{site} (id, awb_number, hawb_number, created_at) VALUES (10, 'same', 'house', '2020')")
            conn.execute(f"INSERT INTO awb_{site} (id, awb_number, created_at) VALUES (50, 'deleted', '2020')")
            conn.execute(f"DELETE FROM awb_{site} WHERE id=50")
            conn.execute(f"CREATE TABLE ref_{site} (awb_id INTEGER REFERENCES awb_{site}(id))")
            conn.execute(f"INSERT INTO ref_{site} VALUES (10)")
            conn.execute(f"CREATE INDEX ix_extra_{site} ON awb_{site}(batch_id)")
            conn.execute(f"CREATE TRIGGER trigger_{site} AFTER UPDATE ON awb_{site} BEGIN SELECT 1; END")
    store = SqliteStore(settings)
    try:
        await store.initialize()
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                first = await store.insert_awb("same", "one")
                second = await store.insert_awb("same", "two")
                assert first.id > 50 and first.id != second.id and first.batch_id == "one" and second.batch_id == "two"
                raw = store._connect()._conn
                raw.execute(f"UPDATE awb_{site} SET hawb_number='house' WHERE id IN (?, ?)", (first.id, second.id))
                raw.commit()
                assert raw.execute(f"SELECT COUNT(*) FROM awb_{site} WHERE hawb_number='house'").fetchone()[0] == 3
                assert raw.execute(f"SELECT awb_id FROM ref_{site}").fetchone()[0] == 10
                assert raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"ix_extra_{site}",)).fetchone()
                assert raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"trigger_{site}",)).fetchone()
                identity, paired = await store.finalize_new(
                    batch_id=f"pair-{site}", track_id=1, embedding=np.ones(64, dtype=np.float32),
                    model_name="m", model_version="v", shape=shape, crop_ref=None,
                    sim_score=None, require_awb=True,
                )
                assert paired.id == 10 and identity.awb_id == 10
                assert {a.id for a in await store.list_awbs()} == {10, first.id, second.id}
            finally:
                warehouse_context.reset(token)
        assert not store._connect()._conn.execute("PRAGMA foreign_key_check").fetchall()
        await store.close()
        await store.initialize()
        assert not store._connect()._conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        await store.close()


def test_invalid_context_fails_closed():
    token = warehouse_context.set("invalid")
    try:
        with pytest.raises(ValueError):
            current_warehouse()
    finally:
        warehouse_context.reset(token)


def test_sql_rewrite_preserves_literals_and_aliases():
    query = "SELECT awb.id, 'FROM awb', 'it''s awb' FROM cv.parcel_identity awb JOIN cv.awb a ON a.id=awb.awb_id -- FROM awb"
    assert warehouse_sql(query, "cas") == (
        "SELECT awb.id, 'FROM awb', 'it''s awb' FROM cv.parcel_identity_cas awb "
        "JOIN cv.awb_cas a ON a.id=awb.awb_id -- FROM awb"
    )
    assert warehouse_sql("SELECT * FROM cv.parcel_identity JOIN cv.awb ON true", "bgd") == (
        "SELECT * FROM cv.parcel_identity_bgd JOIN cv.awb_bgd ON true"
    )


async def test_legacy_sqlite_identity_tables_preserved(settings):
    from app.dedup.sqlite_store import _SCHEMA

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO parcel_identity (batch_id,track_id,status,model_name,model_version,created_at) VALUES ('old',1,'NEW','m','v','2026-01-01')")
        conn.execute("INSERT INTO parcel_embedding (identity_id,model_name,model_version,orientation,embedding) VALUES (1,'m','v',0,?)", (b"vector",))
        conn.execute("INSERT INTO review_audit (identity_id,action,operator,at) VALUES (1,'test','operator','2026-01-01')")
        conn.execute("INSERT INTO session (batch_id,started_at) VALUES ('old','2026-01-01')")
    store = SqliteStore(settings)
    try:
        await store.initialize()
        assert (await store.get(1)).batch_id == "old"
        raw = store._connect()._conn
        for name in ("parcel_identity_bgd", "parcel_embedding_bgd", "review_audit_bgd", "session_bgd"):
            assert raw.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 1
        await store.close()
        await store.initialize()
        assert (await store.get(1)).batch_id == "old"
    finally:
        await store.close()
