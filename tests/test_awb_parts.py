"""QR contract, tenant-scoped manifest resolution, and nullable piece persistence."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.awb.router import AwbBody, router
from app.awb.service import AwbService
from app.dedup.pg_store import PgVectorStore, _WarehouseConnection
from app.dedup.sqlite_store import SqliteStore
from app.events import EventBus
from app.security import require_operator
from app.warehouse import warehouse_context


@pytest.mark.parametrize("field", ["qr_code", "awb_number"])
@pytest.mark.parametrize("raw,hawb,part,total", [
    ("HOUSE_1_1", "HOUSE", 1, 1),
    ("HOUSE_WITH_UNDERSCORES_2_12", "HOUSE_WITH_UNDERSCORES", 2, 12),
    (" HOUSE_01_03 ", "HOUSE", 1, 3),
])
def test_qr_contract(field, raw, hawb, part, total):
    body = AwbBody(**{field: raw})
    assert body.awb_number is None
    assert (body.hawb_number, body.part, body.total) == (hawb, part, total)


@pytest.mark.parametrize("raw", [
    "HOUSE_0_1", "HOUSE_2_1", "HOUSE_-1_2", "HOUSE_1_0", "HOUSE_1_2147483648",
    "HOUSE_x_2", "HOUSE_1_2.0", "HOUSE_1_", "_1_2", "HOUSE__2", "HOUSE_+1_2",
    "HOUSE_١_2", "HOUSE_ 1_2", "HOUSE_1_2_extra",
])
@pytest.mark.parametrize("field", ["qr_code", "awb_number"])
def test_malformed_qr_rejected(field, raw):
    with pytest.raises(ValidationError):
        AwbBody(**{field: raw})


@pytest.mark.parametrize("body", [
    {}, {"qr_code": "HOUSE"}, {"qr_code": "HOUSE_1"}, {"awb_number": "   "},
    {"awb_number": "MASTER", "qr_code": "HOUSE_1_2"},
    {"awb_number": "MASTER", "part": 1}, {"awb_number": "MASTER", "total": 2},
    {"awb_number": "MASTER", "part": 2, "total": 1},
    {"awb_number": "MASTER", "part": 0, "total": 1},
    {"awb_number": "MASTER", "part": "1", "total": 2},
    {"awb_number": "MASTER", "part": True, "total": 2},
    {"awb_number": "MASTER", "part": 1.0, "total": 2},
    {"awb_number": "MASTER", "part": 1, "total": 2147483648},
    {"qr_code": "HOUSE_1_2", "part": 2, "total": 2},
    {"qr_code": "HOUSE_1_2", "hawb_number": "OTHER"},
    {"awb_number": "MASTER", "hawb_number": " "},
])
def test_malformed_explicit_input_rejected(body):
    with pytest.raises(ValidationError):
        AwbBody(**body)


def test_legacy_and_explicit_input():
    legacy = AwbBody(awb_number=" LEGACY-AWB ")
    assert (legacy.awb_number, legacy.part, legacy.total) == ("LEGACY-AWB", None, None)
    explicit = AwbBody(awb_number="MASTER", hawb_number="HOUSE", part=2, total=3)
    assert (explicit.part, explicit.total) == (2, 3)


async def test_router_resolution_and_tenant_isolation(store, settings, monkeypatch):
    candidates = {site: [f"MASTER-{site.upper()}"] for site in ("bgd", "cas")}

    async def manifest_candidates(warehouse, hawb):
        return candidates[warehouse] if hawb == "HOUSE_WITH_UNDERSCORES" else []

    monkeypatch.setattr("app.awb.router.mawb_candidates", manifest_candidates)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_operator] = lambda: None
    app.state.store = store
    app.state.awb = AwbService(settings, store)
    app.state.warehouse_runtimes = {site: SimpleNamespace(bus=EventBus()) for site in ("bgd", "cas")}
    for site, mawb in (("bgd", "MASTER-BGD"), ("cas", "MASTER-CAS")):
        token = warehouse_context.set(site)
        try:
            await store.import_labels([
                (mawb, 3, "HOUSE_WITH_UNDERSCORES", 3, part, None, None, None)
                for part in (1, 2, 3)
            ])
        finally:
            warehouse_context.reset(token)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        ids = []
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                first = None
                for field in ("qr_code", "awb_number"):
                    result = await client.post("/api/awb?warehouse=other", json={field: "HOUSE_WITH_UNDERSCORES_2_3"})
                    assert result.status_code == 200, result.text
                    row = result.json()
                    timestamp = datetime.fromisoformat(row["timecreated"])
                    assert timestamp.utcoffset().total_seconds() == 0
                    assert abs((datetime.now(UTC) - timestamp).total_seconds()) < 30
                    assert (row["awb_number"], row["hawb_number"], row["part"], row["total"]) == (
                        f"MASTER-{site.upper()}", "HOUSE_WITH_UNDERSCORES", 2, 3
                    )
                    if first is None:
                        first = row
                        assert "existing" not in row
                    else:
                        # Rescan HAWB+Part yang sama mengembalikan AWB existing, bukan duplikat.
                        assert row["existing"] is True and row["id"] == first["id"]
                    ids.append(row["id"])
                    assert (await client.get(f"/api/awb/{row['id']}")).json()["part"] == 2
                    assert (await client.get(f"/api/awb/{row['id']}")).json()["timecreated"] == row["timecreated"]
                assert len((await client.get("/api/awb/waiting")).json()["items"]) == 1
                missing = await client.post("/api/awb", json={"qr_code": "UNKNOWN_1_2"})
                assert missing.status_code == 422
                malformed = await client.post("/api/awb", json={"qr_code": "HOUSE_0_2"})
                assert malformed.status_code == 422
                explicit_bad = await client.post("/api/awb", json={"awb_number": "MASTER", "part": 3, "total": 2})
                assert explicit_bad.status_code == 422
            finally:
                warehouse_context.reset(token)
        assert len(set(ids)) == 2
        # A second manifest MAWB for the exact HAWB is ambiguous.
        from app.warehouse import current_warehouse
        candidates[current_warehouse()].append("OTHER")
        ambiguous = await client.post("/api/awb", json={"qr_code": "HOUSE_WITH_UNDERSCORES_1_3"})
        assert ambiguous.status_code == 422 and "multiple MAWBs" in ambiguous.json()["detail"]
        assert len(await store.list_awbs()) == 1
        # The lookup must be exact, not prefix or LIKE matching.
        assert (await client.post("/api/awb", json={"qr_code": "HOUSE_1_3"})).status_code == 422
        legacy = await client.post("/api/awb", json={"awb_number": "LEGACY"})
        assert legacy.status_code == 200
        assert legacy.json()["part"] is None and legacy.json()["total"] is None
        assert legacy.json()["timecreated"] is None
        explicit = await client.post("/api/awb", json={"awb_number": "MASTER", "hawb_number": "HOUSE", "part": 2, "total": 4})
        assert explicit.status_code == 200 and explicit.json()["part"] == 2
        assert explicit.json()["timecreated"] is None


async def test_sqlite_parts_migration_roundtrip_and_pairing(settings, shape):
    # Both pre-010 warehouse tables are upgraded without inventing piece values.
    with sqlite3.connect(settings.sqlite_path) as conn:
        for site in ("bgd", "cas"):
            conn.execute(f"CREATE TABLE awb_{site} (id INTEGER PRIMARY KEY AUTOINCREMENT, awb_number TEXT NOT NULL, hawb_number TEXT, batch_id TEXT, status TEXT DEFAULT 'WAITING', parcel_id INTEGER, paired_at TEXT, created_at TEXT NOT NULL)")
            conn.execute(f"INSERT INTO awb_{site}(awb_number,batch_id,created_at) VALUES('LEGACY','old','2020')")
    store = SqliteStore(settings)
    try:
        await store.initialize()
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                old = (await store.list_awbs())[0]
                assert (old.part, old.total) == (None, None)
                assert old.timecreated is None
                new = await store.insert_awb("MASTER", "new", "HOUSE", 2, 5)
                assert (new.hawb_number, new.part, new.total) == ("HOUSE", 2, 5)
                parcel, paired = await store.finalize_new(
                    batch_id="new", track_id=1, embedding=np.ones(64, dtype=np.float32),
                    model_name="test", model_version="1", shape=shape,
                    crop_ref=None, sim_score=None, require_awb=True,
                )
                assert parcel.awb_id == new.id
                assert (paired.hawb_number, paired.part, paired.total) == ("HOUSE", 2, 5)
            finally:
                warehouse_context.reset(token)
        await store.close()
        await store.initialize()
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                old, new = await store.list_awbs()
                assert (old.part, old.total) == (None, None)
                assert old.timecreated is None
                assert (new.part, new.total, new.status) == (2, 5, "PAIRED")
            finally:
                warehouse_context.reset(token)
    finally:
        await store.close()


@pytest.mark.parametrize("site", ["bgd", "cas"])
async def test_postgres_pairing_returning_includes_parts(site):
    row = dict(id=1, awb_number="MASTER", hawb_number="HOUSE", batch_id="batch",
               status="PAIRED", parcel_id=9, created_at=None, part=2, total=5,
               timecreated=datetime.now(UTC))
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=row), execute=AsyncMock())
    token = warehouse_context.set(site)
    try:
        paired = await PgVectorStore._pair_next_awb_conn(_WarehouseConnection(conn), 9, "batch")
    finally:
        warehouse_context.reset(token)
    query = conn.fetchrow.call_args.args[0]
    assert f"UPDATE cv.awb_{site}" in query
    assert "hawb_number, part, total" in query.split("RETURNING")[1]
    assert "timecreated" in query.split("RETURNING")[1]
    assert paired.timecreated == row["timecreated"]
    assert (paired.part, paired.total, paired.hawb_number) == (2, 5, "HOUSE")


@pytest.mark.parametrize("site", ["bgd", "cas"])
async def test_postgres_insert_list_and_label_sql(settings, monkeypatch, site):
    row = dict(id=1, awb_number="MASTER", hawb_number="HOUSE", batch_id=None,
               status="WAITING", parcel_id=None, created_at=None, part=2, total=5)
    conn = SimpleNamespace(fetch=AsyncMock(side_effect=[[row], [row], [row], [{"mawb": "MASTER"}]]))

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr("app.dedup.pg_store.register_vector", AsyncMock())
    store = PgVectorStore(settings)
    store._pool = SimpleNamespace(acquire=acquire)
    token = warehouse_context.set(site)
    try:
        inserted = await store.insert_awb("MASTER", hawb_number="HOUSE", part=2, total=5)
        listed = (await store.list_awbs())[0]
        waiting = (await store.list_awbs("WAITING"))[0]
        assert inserted == listed == waiting
        assert (inserted.part, inserted.total, inserted.hawb_number) == (2, 5, "HOUSE")
        assert await store.label_mawbs("HOUSE") == ["MASTER"]
    finally:
        warehouse_context.reset(token)
    insert, listing, filtered, lookup = [call.args for call in conn.fetch.call_args_list]
    assert f"INSERT INTO cv.awb_{site}" in insert[0]
    assert insert[1:] == ("MASTER", None, "HOUSE", 2, 5, False)
    assert "timecreated" in insert[0] and "timecreated" in listing[0] and "timecreated" in filtered[0]
    assert "hawb_number, part, total" in insert[0].split("RETURNING")[1]
    assert f"FROM cv.awb_{site}" in listing[0] and "part, total" in listing[0]
    assert "part, total" in filtered[0] and filtered[1:] == ("WAITING",)
    assert f"FROM cv.label_print_{site} WHERE hawb=$1" in lookup[0]
    assert lookup[1:] == ("HOUSE",)


@pytest.mark.parametrize("site", ["bgd", "cas"])
async def test_postgres_qr_timestamp_insert(settings, monkeypatch, site):
    timestamp = datetime.now(UTC)
    row = dict(id=1, awb_number="MASTER", hawb_number="HOUSE", batch_id=None,
               status="WAITING", parcel_id=None, created_at=timestamp, part=1, total=1,
               timecreated=timestamp)
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[row]))

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr("app.dedup.pg_store.register_vector", AsyncMock())
    store = PgVectorStore(settings)
    store._pool = SimpleNamespace(acquire=acquire)
    token = warehouse_context.set(site)
    try:
        result = await store.insert_awb("MASTER", hawb_number="HOUSE", part=1, total=1, qr_scanned=True)
    finally:
        warehouse_context.reset(token)
    query, *args = conn.fetch.call_args.args
    assert f"INSERT INTO cv.awb_{site}" in query
    assert "CASE WHEN $6::boolean THEN now() ELSE NULL END" in query
    assert args[-1] is True and result.timecreated == timestamp
