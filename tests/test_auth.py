import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth_store import AuthStore, hash_password, verify_password
from app.config import DedupConfig, Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, jwt_secret="a" * 32, bootstrap_password="admin123",
                        sqlite_path=str(tmp_path / "test.sqlite"), crop_dir=tmp_path / "crops",
                        dedup=DedupConfig(store="sqlite"), stream_token="legacy")
    app = create_app(settings)
    store = AuthStore(settings, None)
    asyncio.run(store.initialize())
    app.state.auth_store = store
    app.state.source_mgr = SimpleNamespace(push_screen=lambda view, data: None)
    app.state.warehouse_runtimes = {"cas": SimpleNamespace(source_mgr=app.state.source_mgr)}
    yield TestClient(app, base_url="https://testserver")
    store.close()


def test_provisioning_is_hashed_and_idempotent(client):
    store = client.app.state.auth_store
    user = asyncio.run(store.get("cas@paratekno.com"))
    assert user["password_hash"] != "admin123"
    assert verify_password("admin123", user["password_hash"])
    changed = hash_password("changed")
    store.sqlite.execute("UPDATE auth_users SET password_hash=? WHERE email=?", (changed, user["email"]))
    store.sqlite.commit()
    store.close()
    replacement = AuthStore(client.app.state.settings, None)
    asyncio.run(replacement.initialize())
    client.app.state.auth_store = replacement
    assert asyncio.run(replacement.get(user["email"]))["password_hash"] == changed
    replacement.close()
    assert sqlite3.connect(client.app.state.settings.sqlite_path).execute("SELECT count(*) FROM auth_users").fetchone()[0] == 2


def test_login_cookie_logout_and_websocket(client):
    assert client.get("/api/auth/session").json() == {"authenticated": False, "user": None}
    assert client.get("/api/session/state", headers={"x-operator-token": "legacy"}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver/ws/push/top?token=legacy"):
            pass
    assert client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"}, headers={"origin": "https://evil.test"}).status_code == 403
    response = client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"})
    assert response.json() == {"authenticated": True, "user": {"email": "cas@paratekno.com", "access": "cas"}}
    assert all(v in response.headers["set-cookie"].lower() for v in ("httponly", "samesite=strict", "secure"))
    assert client.get("/api/auth/session").json() == response.json()
    assert client.get("/crops/legacy.jpg").status_code == 403
    assert client.get("/crops/cas/%2e%2e%2flegacy.jpg").status_code == 403
    with client.websocket_connect("wss://testserver/ws/push/top") as ws:
        ws.send_bytes(b"frame")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver/ws/push/top", headers={"origin": "https://evil.test"}):
            pass
    token = client.cookies.get("cv_session")
    assert client.post("/api/auth/logout").json() == {"authenticated": False, "user": None}
    client.cookies.set("cv_session", token, domain="testserver.local", path="/")
    assert client.get("/api/session/state").status_code == 401
    # Logout wajib tetap berhasil dengan cookie kedaluwarsa/invalid — pengguna tidak boleh terjebak.
    client.cookies.set("cv_session", "garbage", domain="testserver.local", path="/")
    assert client.post("/api/auth/logout").json() == {"authenticated": False, "user": None}


def test_bad_token_and_bgd_access(client):
    client.cookies.set("cv_session", "invalid", domain="testserver.local", path="/")
    assert client.get("/api/auth/session").json()["authenticated"] is False
    result = client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"})
    assert result.json()["user"]["access"] == "bgd"
    assert client.get("/crops/cas/other.jpg").status_code == 403
    assert client.get("/crops/CAS/other.jpg").status_code == 403


def test_spa_assets_cannot_escape_ui_directory(client):
    response = client.get("/%2e%2e/%2e%2e/.env")
    assert response.status_code == 404


def test_session_refreshes_when_near_expiry_and_caps_at_three_months(client):
    import jwt as pyjwt
    import time as time_module
    from app.security import COOKIE, REFRESH_AFTER, SESSION_SECONDS, issue_session

    settings = client.app.state.settings
    user = {"email": "cas@paratekno.com", "access": "cas"}
    now = int(time_module.time())
    aging = pyjwt.encode({"sub": user["email"], "access": "cas", "iat": now - SESSION_SECONDS + 1000,
                          "exp": now + 1000, "jti": "aging-jti"}, settings.jwt_secret, algorithm="HS256")
    client.cookies.set(COOKIE, aging, domain="testserver.local", path="/")
    response = client.get("/api/auth/session")
    assert response.json()["authenticated"] is True
    fresh = client.cookies.get(COOKIE)
    assert fresh != aging
    claims = pyjwt.decode(fresh, settings.jwt_secret, algorithms=["HS256"])
    assert claims["jti"] != "aging-jti"
    assert claims["exp"] - claims["iat"] == SESSION_SECONDS
    assert SESSION_SECONDS == 90 * 24 * 60 * 60
    assert REFRESH_AFTER < SESSION_SECONDS
    client.cookies.set(COOKIE, aging, domain="testserver.local", path="/")
    assert client.get("/api/session/state").status_code == 401  # token lama sudah digulirkan


def test_fresh_session_is_not_rotated(client):
    import jwt as pyjwt
    from app.security import COOKIE

    client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"})
    token = client.cookies.get(COOKIE)
    client.get("/api/auth/session")
    assert client.cookies.get(COOKIE) == token  # masih panjang umur, tidak digulirkan


def test_authenticated_warehouse_awb_isolation(tmp_path, monkeypatch):
    from tests.conftest import StubEmbedder
    import app.main as main
    import app.awb.router as awb_router

    async def manifest_lookup(warehouse, mawb, hawb):
        return {"status": "found", "data": {"warehouse": warehouse}}

    async def manifest_candidates(warehouse, hawb):
        return []

    monkeypatch.setattr(awb_router, "fetch_manifest", manifest_lookup)
    monkeypatch.setattr(awb_router, "mawb_candidates", manifest_candidates)

    monkeypatch.setattr(main, "create_embedder", lambda _: StubEmbedder())
    settings = Settings(_env_file=None, jwt_secret="a" * 32, bootstrap_password="admin123",
                        sqlite_path=str(tmp_path / "integrated.sqlite"), crop_dir=tmp_path / "crops",
                        dedup=DedupConfig(store="sqlite"), warehouse_bgd_cameras="0", warehouse_cas_cameras="1")
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/awb/qr-check", json={"qr_code": "HOUSE_1_2"}).status_code == 401
        assert client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get("/api/sources").json()["cameras"] == ["camera:0"]
        assert client.post("/api/sources", json={"view": "top", "kind": "camera:1"}).status_code == 403
        bgd = client.post("/api/awb", json={"awb_number": "BGD-TEST"}).json()
        assert client.get(f"/api/awb/{bgd['id']}/manifest?warehouse=cas").json()["data"]["warehouse"] == "bgd"
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get("/api/sources").json()["cameras"] == ["camera:1"]
        assert client.post("/api/sources", json={"view": "top", "kind": "camera:0"}).status_code == 403
        assert client.get("/api/awb").json()["items"] == []
        assert client.get(f"/api/awb/{bgd['id']}").status_code == 404
        assert client.get(f"/api/awb/{bgd['id']}/manifest").status_code == 404
        cas = client.post("/api/awb", json={"awb_number": "CAS-TEST"}).json()
        assert client.get(f"/api/awb/{cas['id']}/manifest?warehouse=bgd").json()["data"]["warehouse"] == "cas"
        assert [item["awb_number"] for item in client.get("/api/awb").json()["items"]] == ["CAS-TEST"]
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get(f"/api/awb/{cas['id']}").status_code == 404
        assert [item["awb_number"] for item in client.get("/api/awb").json()["items"]] == ["BGD-TEST"]
        assert client.post("/api/session/start").status_code == 200
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"})
        assert client.get("/api/session/state").json()["running"] is False
        assert client.post("/api/session/start").status_code == 200
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bgd@paratekno.com", "password": "admin123"})
        assert client.get("/api/session/state").json()["running"] is True
        assert client.post("/api/session/stop").status_code == 200

        # Label fallback must use the authenticated warehouse, not the query parameter.
        from app.warehouse import warehouse_context

        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                asyncio.run(client.app.state.store.import_labels([
                    (f"MASTER-{site}", 2, "SHARED_HOUSE", 2, 1, None, None, None),
                ]))
            finally:
                warehouse_context.reset(token)
        scanned = {}
        for site in ("bgd", "cas"):
            client.post("/api/auth/logout")
            client.post("/api/auth/login", json={"email": f"{site}@paratekno.com", "password": "admin123"})
            check = client.post("/api/awb/qr-check?warehouse=other", json={"qr_code": "SHARED_HOUSE_1_2"})
            assert check.status_code == 200
            assert check.json()["mawb"] == f"MASTER-{site}" and check.json()["existing"] is False
            proof = check.json()["proof_token"]
            if site == "cas":
                rejected = client.post("/api/awb", json={"qr_code": "SHARED_HOUSE_1_2", "proof_token": bgd_proof})
                assert rejected.status_code == 422
            else:
                bgd_proof = proof
            response = client.post("/api/awb?warehouse=other", json={"qr_code": "SHARED_HOUSE_1_2", "proof_token": proof})
            assert response.status_code == 200
            scanned[site] = response.json()
            assert (scanned[site]["awb_number"], scanned[site]["part"], scanned[site]["total"]) == (f"MASTER-{site}", 1, 2)
            check = client.post("/api/awb/qr-check?warehouse=other", json={"qr_code": "SHARED_HOUSE_1_2"})
            assert check.json()["awb"]["id"] == scanned[site]["id"]
        assert scanned["bgd"]["id"] != scanned["cas"]["id"]
        assert client.get(f"/api/awb/{scanned['bgd']['id']}").status_code == 404
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"})
        assert client.get("/api/session/state").json()["running"] is True
        assert client.post("/api/session/stop").status_code == 200
