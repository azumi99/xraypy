"""Review/resolve flow + audit trail."""

from __future__ import annotations

import pytest

from tests.conftest import make_bgra


async def _make_review(dedup, store):
    """Force a REVIEW parcel via gray zone: insert seed parcel then a fixed-sim search."""
    from tests.test_dedup import FixedSimStore

    svc_store = FixedSimStore(store, 0.85)
    from app.dedup.service import DedupService

    svc = DedupService(dedup._settings, dedup._worker, svc_store)
    d = await svc.process_parcel(batch_id="rb", track_id=77, crop_bgra=make_bgra())
    assert d.status == "REVIEW"
    return d


async def test_not_duplicate_promotes_to_new_with_idx(dedup, store):
    await store.insert_awb("RW-1")
    d = await _make_review(dedup, store)
    record, awb = await dedup.resolve_review(d.record.id, "NOT_DUPLICATE", "op1")
    assert record.status == "NEW"
    assert record.idx == 0
    assert awb is not None and awb.awb_number == "RW-1"


async def test_confirm_duplicate_sets_status(dedup, store):
    d = await _make_review(dedup, store)
    record, _ = await dedup.resolve_review(d.record.id, "CONFIRM_DUPLICATE", "op1", "sama persis")
    assert record.status == "DUPLICATE"
    assert record.idx is None


async def test_invalid_action_rejected(dedup):
    with pytest.raises(ValueError):
        await dedup.resolve_review(1, "SOMETHING_ELSE", "op1")


async def test_pending_reviews_lists(dedup, store):
    before = len(await store.pending_reviews())
    await _make_review(dedup, store)
    after = await store.pending_reviews()
    assert len(after) == before + 1


async def test_awb_endpoints_api(dedup, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.awb.router import router
    from app.awb.service import AwbService
    from app.events import EventBus

    app = FastAPI()
    from app.security import require_operator
    app.dependency_overrides[require_operator] = lambda: None
    app.include_router(router)
    app.state.awb = AwbService(dedup._settings, store)
    app.state.bus = EventBus()
    app.state.source_mgr = None
    app.state.pipeline = None
    app.state.store = store

    class _S:
        stream_token = None

    app.state.settings = _S()
    client = TestClient(app)
    r = client.post("/awb", json={"awb_number": "TEST-1"})
    assert r.status_code == 200
    assert r.json()["status"] == "WAITING"
    r2 = client.post("/awb", json={"awb_number": "TEST-1"})
    assert r2.status_code == 200
    assert r2.json()["id"] != r.json()["id"]
    r3 = client.get("/awb/waiting")
    assert r3.status_code == 200
    assert any(a["awb_number"] == "TEST-1" for a in r3.json()["items"])


async def test_delete_awb_waiting(dedup, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.awb.router import router
    from app.awb.service import AwbService
    from app.events import EventBus

    await store.insert_awb("DEL-1")
    app = FastAPI()
    from app.security import require_operator
    app.dependency_overrides[require_operator] = lambda: None
    app.include_router(router)
    app.state.awb = AwbService(dedup._settings, store)
    app.state.bus = EventBus()
    app.state.source_mgr = None
    app.state.pipeline = None
    app.state.store = store

    class _S:
        stream_token = None

    app.state.settings = _S()
    client = TestClient(app)

    awb_id = next(a.id for a in await store.list_awbs() if a.awb_number == "DEL-1")
    r = client.delete(f"/awb/{awb_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert all(a.awb_number != "DEL-1" for a in await store.list_awbs())  # gone from DB

    r2 = client.delete(f"/awb/{awb_id}")  # already gone
    assert r2.status_code == 404


async def test_delete_awb_paired_refused(dedup, store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.awb.router import router
    from app.awb.service import AwbService
    from app.events import EventBus

    await store.insert_awb("PAIRED-1")
    from tests.conftest import make_bgra

    d = await dedup.process_parcel(batch_id="delb", track_id=1, crop_bgra=make_bgra(seed=1))
    assert d.awb is not None and d.awb.awb_number == "PAIRED-1"
    awb_id = d.awb.id

    app = FastAPI()
    from app.security import require_operator
    app.dependency_overrides[require_operator] = lambda: None
    app.include_router(router)
    app.state.awb = AwbService(dedup._settings, store)
    app.state.bus = EventBus()
    app.state.source_mgr = None
    app.state.pipeline = None
    app.state.store = store

    class _S:
        stream_token = None

    app.state.settings = _S()
    client = TestClient(app)
    # list now embeds parcel detail (for the AWB queue UI: photo + info)
    listing = client.get("/awb").json()["items"]
    entry = next(a for a in listing if a["awb_number"] == "PAIRED-1")
    assert entry["parcel"]["id"] == d.record.id
    assert "crop_ref" in entry["parcel"]

    r = client.delete(f"/awb/{awb_id}")
    assert r.status_code == 409  # paired without force -> refused
    assert any(a.awb_number == "PAIRED-1" for a in await store.list_awbs())

    # force: unlinks the parcel (becomes AWB-less), deletes the AWB, audits it
    r2 = client.delete(f"/awb/{awb_id}?force=true")
    assert r2.status_code == 200 and r2.json()["force"] is True
    assert all(a.awb_number != "PAIRED-1" for a in await store.list_awbs())
    rec = await store.get(d.record.id)
    assert rec.awb_id is None and rec.awb_number is None  # parcel survives, unlinked
    audits = await store.pending_reviews()  # smoke: store still consistent
    assert isinstance(audits, list)
