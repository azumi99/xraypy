import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI

from app.awb.router import router
from app.awb.service import AwbService
from app.events import EventBus
from app.security import require_operator
from app.warehouse import warehouse_context


@pytest.fixture
def app(store, settings, monkeypatch):
    from app.awb import router as router_module

    settings.jwt_secret = "test-secret-" * 4
    monkeypatch.setattr(router_module, "identity", lambda request: {"email": "operator@example.com", "access": "bgd"})
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_operator] = lambda: None
    app.state.store = store
    app.state.awb = AwbService(settings, store)
    app.state.bus = EventBus()
    app.state.source_mgr = None
    app.state.pipeline = None
    app.state.settings = settings
    app.state.dedup = None
    yield app
    for runtime in getattr(app.state, "warehouse_runtimes", {}).values():
        if runtime.source_mgr is not None:
            runtime.source_mgr.close()


async def test_rescan_same_hawb_part_returns_existing(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["273-07021453"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        await store.import_labels([("273-07021453", 155, "80004159", 155, 1, None, None, None)])
        first = (await client.post("/api/awb", json={"qr_code": "80004159_3_155"})).json()
        assert "existing" not in first
        again = (await client.post("/api/awb", json={"qr_code": "80004159_3_155"})).json()
        assert again["existing"] is True and again["id"] == first["id"]
        other_part = (await client.post("/api/awb", json={"qr_code": "80004159_4_155"})).json()
        assert "existing" not in other_part and other_part["id"] != first["id"]


async def test_simultaneous_qr_scans_create_only_one_awb(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["MASTER"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        results = await asyncio.gather(*(client.post("/api/awb", json={"qr_code": "HOUSE_2_3"}) for _ in range(4)))
        assert all(result.status_code == 200 for result in results)
        rows = [result.json() for result in results]
        assert len({row["id"] for row in rows}) == 1
        assert sum(row.get("existing", False) for row in rows) == 3
        assert len(await store.list_awbs()) == 1


async def test_manifest_mawb_used_to_pick_existing(app, store, monkeypatch):
    from app.awb import router as router_module

    async def fake_candidates(warehouse, hawb):
        return ["273-07021453"] if hawb == "80004159" else []

    monkeypatch.setattr(router_module, "mawb_candidates", fake_candidates)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        created = (await client.post("/api/awb", json={"qr_code": "80004159_3_155"})).json()
        assert created["awb_number"] == "273-07021453"
        async def other_manifest(warehouse, hawb):
            return ["OTHER-MASTER"]

        monkeypatch.setattr(router_module, "mawb_candidates", other_manifest)
        await store.import_labels([("OTHER-MASTER", 155, "80004159", 155, 1, None, None, None)])
        rescan = (await client.post("/api/awb", json={"qr_code": "80004159_3_155"})).json()
        assert "existing" not in rescan and rescan["awb_number"] == "OTHER-MASTER"
        assert rescan["id"] != created["id"]


async def test_ambiguous_manifest_mawb_rejected(app, store, monkeypatch):
    from app.awb import router as router_module

    async def ambiguous(warehouse, hawb):
        return ["M1", "M2"]

    monkeypatch.setattr(router_module, "mawb_candidates", ambiguous)
    await store.import_labels([("LABEL", 5, "80004159", 5, 1, None, None, None)])
    labels = AsyncMock(wraps=store.label_mawbs)
    monkeypatch.setattr(store, "label_mawbs", labels)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ("/api/awb", "/api/awb/qr-check"):
            response = await client.post(path, json={"qr_code": "80004159_1_5"})
            assert response.status_code == 422
            assert "multiple MAWBs" in response.json()["detail"]
    labels.assert_not_awaited()


async def test_existing_check_scoped_to_warehouse(app, store, monkeypatch, settings):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["273-07021453"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        await store.import_labels([("273-07021453", 155, "80004159", 155, 1, None, None, None)])
        token = warehouse_context.set("bgd")
        try:
            bgd = (await client.post("/api/awb", json={"qr_code": "80004159_2_155"})).json()
        finally:
            warehouse_context.reset(token)
        token = warehouse_context.set("cas")
        try:
            await store.import_labels([("273-07021453", 155, "80004159", 155, 1, None, None, None)])
            cas = (await client.post("/api/awb", json={"qr_code": "80004159_2_155"})).json()
        finally:
            warehouse_context.reset(token)
        assert "existing" not in cas and cas["id"] != bgd["id"]


async def test_qr_check_exact_match_read_only(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["MASTER"]))
    other = await store.insert_awb("OTHER", hawb_number="HOUSE", part=1, total=3)
    publish = AsyncMock()
    monkeypatch.setattr(app.state.bus, "publish", publish)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})
        result = response.json()
        assert result.pop("proof_token")
        assert result == {"existing": False, "mawb": "MASTER", "hawb_number": "HOUSE",
                                   "part": 1, "total": 3, "awb": None}
        assert [a.id for a in await store.list_awbs()] == [other.id]
        created = (await client.post("/api/awb", json={"qr_code": "HOUSE_1_3"})).json()
        publish.reset_mock()
        response = await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_01_03"})
        assert response.status_code == 200
        result = response.json()
        assert result.pop("proof_token")
        assert result == {"existing": True, "mawb": "MASTER", "hawb_number": "HOUSE",
                                   "part": 1, "total": 3, "awb": created}
        assert len(await store.list_awbs()) == 2
        publish.assert_not_awaited()
        assert (await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_4_3"})).status_code == 422
        assert (await client.post("/api/awb/qr-check", json={})).status_code == 422


async def test_qr_exact_duplicates_rejected_for_check_and_create(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["MASTER"]))
    for _ in range(2):
        await store.insert_awb("MASTER", hawb_number="HOUSE", part=1, total=3)
    publish = AsyncMock()
    monkeypatch.setattr(app.state.bus, "publish", publish)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ("/api/awb/qr-check", "/api/awb"):
            response = await client.post(path, json={"qr_code": "HOUSE_1_3"})
            assert response.status_code == 409 and "Multiple AWBs" in response.json()["detail"]
        assert len(await store.list_awbs()) == 2
        publish.assert_not_awaited()


async def test_proof_skips_resolution_and_serializes_duplicates(app, store, monkeypatch):
    from app.awb import router as router_module

    lookup = AsyncMock(return_value=["MASTER"])
    monkeypatch.setattr(router_module, "mawb_candidates", lookup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        proof = (await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})).json()["proof_token"]
        results = await asyncio.gather(*(client.post("/api/awb", json={"qr_code": "HOUSE_1_3", "proof_token": proof}) for _ in range(4)))
        assert all(r.status_code == 200 for r in results)
        assert len({r.json()["id"] for r in results}) == 1
        assert sum(r.json().get("existing", False) for r in results) == 3
        assert len(await store.list_awbs()) == 1
        lookup.assert_awaited_once()


async def test_proof_rejects_invalid_context_and_expiry(app, store, monkeypatch):
    from app.awb import router as router_module

    lookup = AsyncMock(return_value=["MASTER"])
    monkeypatch.setattr(router_module, "mawb_candidates", lookup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        proof = (await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})).json()["proof_token"]
        claims = jwt.decode(proof, app.state.settings.jwt_secret, algorithms=["HS256"])
        expired = dict(claims, iat=int(time.time()) - 301, exp=int(time.time()) - 1)
        wrong_purpose = dict(claims, purpose="other")
        invalid_tokens = [proof + "x", jwt.encode(expired, app.state.settings.jwt_secret, algorithm="HS256"),
                          jwt.encode(wrong_purpose, app.state.settings.jwt_secret, algorithm="HS256")]
        for token in invalid_tokens:
            response = await client.post("/api/awb", json={"qr_code": "HOUSE_1_3", "proof_token": token})
            assert response.status_code == 422 and "qr-check" in response.json()["detail"]
        assert (await client.post("/api/awb", json={"qr_code": "HOUSE_2_3", "proof_token": proof})).status_code == 422
        assert (await client.post("/api/awb", json={"awb_number": "MASTER", "proof_token": proof})).status_code == 422
        site = warehouse_context.set("cas")
        try:
            assert (await client.post("/api/awb", json={"qr_code": "HOUSE_1_3", "proof_token": proof})).status_code == 422
        finally:
            warehouse_context.reset(site)
        monkeypatch.setattr(router_module, "identity", lambda request: {"email": "other@example.com", "access": "bgd"})
        assert (await client.post("/api/awb", json={"qr_code": "HOUSE_1_3", "proof_token": proof})).status_code == 422
        monkeypatch.setattr(router_module, "identity", lambda request: None)
        assert (await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})).status_code == 401
        assert (await client.post("/api/awb", json={"qr_code": "HOUSE_1_3", "proof_token": proof})).status_code == 401
        assert await store.list_awbs() == []
        lookup.assert_awaited_once()


async def test_manifest_unavailable_does_not_fall_back(app, store, monkeypatch):
    from app.awb import router as router_module

    lookup = AsyncMock(return_value=None)
    labels = AsyncMock(wraps=store.label_mawbs)
    monkeypatch.setattr(router_module, "mawb_candidates", lookup)
    monkeypatch.setattr(store, "label_mawbs", labels)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                await store.import_labels([("MASTER", 3, "HOUSE", 3, 1, None, None, None)])
                for path in ("/api/awb/qr-check", "/api/awb"):
                    response = await client.post(path, json={"qr_code": "HOUSE_1_3"})
                    assert response.status_code == 503
                assert await store.list_awbs() == []
            finally:
                warehouse_context.reset(token)
    labels.assert_not_awaited()
    assert [call.args for call in lookup.await_args_list] == [
        (site, "HOUSE") for site in ("bgd", "cas") for _ in range(2)
    ]


async def test_label_fallback_is_unique_and_warehouse_scoped(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=[]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                # Multiple printed pieces for one MAWB are still a unique match.
                await store.import_labels([
                    (f"MASTER-{site}", 3, "HOUSE", 3, part, None, None, None)
                    for part in (1, 2)
                ])
                check = await client.post("/api/awb/qr-check?warehouse=other", json={"qr_code": "HOUSE_1_3"})
                assert check.status_code == 200 and check.json()["mawb"] == f"MASTER-{site}"
                assert check.json()["existing"] is False
                created = await client.post("/api/awb?warehouse=other", json={"qr_code": "HOUSE_1_3"})
                assert created.status_code == 200 and created.json()["awb_number"] == f"MASTER-{site}"
                assert (await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})).json()["existing"] is True
            finally:
                warehouse_context.reset(token)


async def test_missing_and_ambiguous_label_fallback_rejected(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=[]))
    await store.import_labels([
        (mawb, 3, "HOUSE", 3, 1, None, None, None) for mawb in ("M1", "M2")
    ])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ("/api/awb/qr-check", "/api/awb"):
            assert (await client.post(path, json={"qr_code": "UNKNOWN_1_3"})).status_code == 422
            response = await client.post(path, json={"qr_code": "HOUSE_1_3"})
            assert response.status_code == 422 and "multiple MAWBs" in response.json()["detail"]
        assert await store.list_awbs() == []


async def test_manifest_match_preferred_over_labels(app, store, monkeypatch):
    from app.awb import router as router_module

    monkeypatch.setattr(router_module, "mawb_candidates", AsyncMock(return_value=["MANIFEST"]))
    await store.import_labels([("LABEL", 3, "HOUSE", 3, 1, None, None, None)])
    labels = AsyncMock(wraps=store.label_mawbs)
    monkeypatch.setattr(store, "label_mawbs", labels)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        check = await client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_3"})
        assert check.status_code == 200 and check.json()["mawb"] == "MANIFEST"
        created = await client.post("/api/awb", json={"qr_code": "HOUSE_1_3"})
        assert created.status_code == 200 and created.json()["awb_number"] == "MANIFEST"
    labels.assert_not_awaited()


def test_manifest_binary_mawb_decoded(monkeypatch):
    from app.awb import manifest

    class Connection:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, args):
            assert "BINARY `MasterAWB`" in query and args == ("HOUSE",)

        def fetchall(self):
            return [{"mawb": b"MASTER"}]

        def close(self):
            pass

    monkeypatch.setattr(manifest, "_connect", lambda warehouse: Connection())
    assert manifest._mawb_lookup("bgd", "HOUSE") == ["MASTER"]
