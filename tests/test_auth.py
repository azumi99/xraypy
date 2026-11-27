import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth_store import AuthStore, hash_password, verify_password
from app.config import DedupConfig, Settings
from app.dedup.sqlite_store import SqliteStore
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

    manifest_dates = {"bgd": "2026-11-24T10:00:00", "cas": "2026-11-25T11:00:00"}

    async def manifest_lookup(warehouse, mawb, hawb):
        return {"status": "found", "data": {"warehouse": warehouse, "created_at": manifest_dates[warehouse]}}

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
        manifest_data = client.get(f"/api/awb/{bgd['id']}/manifest?warehouse=cas").json()["data"]
        assert manifest_data == {"warehouse": "bgd", "created_at": manifest_dates["bgd"]}
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"email": "cas@paratekno.com", "password": "admin123"}).status_code == 200
        assert client.get("/api/sources").json()["cameras"] == ["camera:1"]
        assert client.post("/api/sources", json={"view": "top", "kind": "camera:0"}).status_code == 403
        assert client.get("/api/awb").json()["items"] == []
        assert client.get(f"/api/awb/{bgd['id']}").status_code == 404
        assert client.get(f"/api/awb/{bgd['id']}/manifest").status_code == 404
        cas = client.post("/api/awb", json={"awb_number": "CAS-TEST"}).json()
        manifest_data = client.get(f"/api/awb/{cas['id']}/manifest?warehouse=bgd").json()["data"]
        assert manifest_data == {"warehouse": "cas", "created_at": manifest_dates["cas"]}
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


def test_web_login_validates_turnstile(client, monkeypatch):
    from unittest.mock import AsyncMock

    settings = client.app.state.settings
    settings.turnstile_site_key = "site-key"
    settings.turnstile_secret_key = "secret-key"
    response = client.get("/api/auth/captcha")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"site_key": "site-key"}

    body = {"email": "cas@paratekno.com", "password": "admin123"}
    assert client.post("/api/auth/web-login", json=body).status_code == 422
    result = {"success": False}
    verify = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: result))
    context = AsyncMock()
    context.__aenter__.return_value = SimpleNamespace(post=verify)
    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **kwargs: context)
    assert client.post("/api/auth/web-login", json={**body, "captcha_token": "test"}).status_code == 422
    result.update(success=True, action="web_login")
    assert client.post("/api/auth/web-login", json={**body, "captcha_token": "test"}).status_code == 200


@pytest.mark.parametrize("warehouse", ["bgd", "cas"])
def test_bc_account_has_warehouse_scoped_read_only_access(client, warehouse):
    settings = client.app.state.settings
    setattr(settings, f"bc_{warehouse}_password", "test-bc-password")
    store = AuthStore(settings, None)
    asyncio.run(store.initialize())
    store.close()
    login = client.post("/api/auth/web-login", json={
        "email": f"bc-{warehouse}@paratekno.com", "password": "test-bc-password",
    })
    assert login.status_code == 200
    assert client.get("/api/auth/session").json()["user"]["role"] == "bc"
    assert client.get("/api/auth/session").json()["user"]["access"] == warehouse
    assert client.delete("/api/awb/1?force=true").status_code == 403
    assert client.post("/api/session/start").status_code == 403
    assert client.get("/api/settings/tunnels").status_code == 403
    assert client.get("/live", follow_redirects=False).headers["location"] == f"/{warehouse}/dashboard"
    opposite_crop = "/crops/other.png" if warehouse == "cas" else "/crops/cas/other.png"
    assert client.get(opposite_crop).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver/ws/push/top"):
            pass


@pytest.mark.parametrize("warehouse", ["bgd", "cas"])
def test_label_print_role_is_web_only_and_can_list_labels(client, warehouse, tmp_path):
    settings = client.app.state.settings
    setattr(settings, f"label_print_{warehouse}_password", "test-label-password")
    store = AuthStore(settings, None)
    asyncio.run(store.initialize())
    store.close()

    credentials = {"email": f"label-print-{warehouse}@paratekno.com", "password": "test-label-password"}
    assert client.post("/api/auth/login", json=credentials).status_code == 403
    assert client.post("/api/auth/web-login", json=credentials).json()["user"]["role"] == "label_print"

    label_store = SqliteStore(settings, tmp_path / "labels.sqlite")
    asyncio.run(label_store.initialize())
    client.app.state.store = label_store
    from app.warehouse import warehouse_context
    from tests.test_labels import workbook_bytes

    email = credentials["email"]
    assert client.get("/api/labels").json() == []
    assert client.post("/api/labels/import").status_code == 403
    assert client.post("/api/labels/delete-many", json={"ids": [1]}).status_code == 403
    assert asyncio.run(client.app.state.auth_store.update_user(
        email, warehouse, "label_print", True, None, "PJT-A"
    ))
    assert client.post("/api/auth/web-login", json=credentials).json()["user"]["pjt_code"] == "PJT-A"
    assert client.get("/api/auth/session").json()["user"]["pjt_code"] == "PJT-A"
    for site in ("cas", "bgd"):
        token = warehouse_context.set(site)
        try:
            asyncio.run(label_store.import_labels(
                [(site, 1, "H1", 1, 1, "", "", "")], "PJT-A"
            ))
            asyncio.run(label_store.import_labels(
                [(site, 1, "H2", 1, 1, "", "", "")], "PJT-B"
            ))
            asyncio.run(label_store.import_labels(
                [(site, 1, "H3", 1, 1, "", "", "")]
            ))
        finally:
            warehouse_context.reset(token)
    operator = TestClient(client.app, base_url="https://testserver")
    try:
        assert operator.post("/api/auth/web-login", json={
            "email": f"{warehouse}@paratekno.com", "password": settings.bootstrap_password,
        }).status_code == 200
        assert operator.get("/api/labels/pjt-codes").json() == ["PJT-A"]
        files = {"file": ("labels.xlsx", workbook_bytes(pcs=1))}
        assert operator.post("/api/labels/import", data={"pjt_code": "PJT-B"}, files=files).status_code == 422
        assert operator.post("/api/labels/import", data={"pjt_code": "pjt-a"}, files=files).status_code == 200
    finally:
        operator.close()
    template = client.get("/api/labels/template")
    assert template.status_code == 200
    from app.labels import parse_workbook
    assert parse_workbook(template.content) == [
        ("273-07001551", 8, "WFCGK61108", 8, part, "Garuda Indonesia", "SZX", "SZX-CGK")
        for part in range(1, 9)
    ]
    files = {"file": ("labels.xlsx", workbook_bytes(pcs=1, mawb="PJT-MAWB"))}
    assert client.post("/api/labels/import", data={"pjt_code": "PJT-B"}, files=files).status_code == 403
    assert client.post("/api/labels/import", files=files).json()["imported"] == 1
    for hawb in ("H2", "H3"):
        files = {"file": ("labels.xlsx", workbook_bytes(pcs=1, mawb=warehouse, hawb=hawb))}
        assert client.post("/api/labels/import", files=files).json()["imported"] == 1
        assert client.post("/api/labels/import", files=files).json()["updated"] == 1
    result = client.get("/api/labels?pjt_code=PJT-B").json()
    token = warehouse_context.set(warehouse)
    try:
        all_labels = asyncio.run(label_store.list_labels())
    finally:
        warehouse_context.reset(token)
    assert {(row["pjt_code"], row["hawb"]) for row in all_labels if row["mawb"] == warehouse} == {
        ("PJT-A", "H1"), ("PJT-A", "H2"), ("PJT-A", "H3"), ("PJT-B", "H2"), (None, "H3"),
    }
    assert {(row["mawb"], row["hawb"]) for row in result} == {
        (warehouse, "H1"), (warehouse, "H2"), (warehouse, "H3"), ("M1", "H1"), ("PJT-MAWB", "H1"),
    }
    assert client.get("/api/awb").status_code == 403
    own_id = next(row["id"] for row in result if row["mawb"] == "PJT-MAWB")
    foreign_ids = {row["id"] for row in all_labels if row["pjt_code"] != "PJT-A"}
    assert client.post("/api/labels/delete-many", json={"ids": [own_id, *foreign_ids]}).json() == {"deleted": 1}
    assert own_id not in {row["id"] for row in client.get("/api/labels").json()}
    token = warehouse_context.set(warehouse)
    try:
        assert foreign_ids <= {row["id"] for row in asyncio.run(label_store.list_labels())}
    finally:
        warehouse_context.reset(token)
        asyncio.run(label_store.close())
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == f"/{warehouse}/labels"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver/ws/stream"):
            pass


def test_global_admin_manages_users_and_invalidates_previous_sessions(client):
    settings = client.app.state.settings
    settings.admin_email = "developer@example.com"
    settings.admin_password = "developer-test-password"
    asyncio.run(client.app.state.auth_store.initialize())
    credentials = {"email": settings.admin_email, "password": settings.admin_password}
    assert client.post("/api/auth/login", json=credentials).status_code == 403
    assert client.post("/api/auth/web-login", json=credentials).json()["user"]["role"] == "admin"
    assert client.get("/api/awb").status_code == 403
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/bgd/users"
    email = "new.operator@example.com"
    password = "initial-test-password"
    account = {"email": email, "password": password, "role": "operator", "access": "cas"}
    assert client.post("/api/users", json=account).status_code == 201
    assert client.post("/api/users", json=account).status_code == 409
    listed = client.get("/api/users").json()
    assert {"email": email, "role": "operator", "access": "cas", "active": True} in listed
    assert all("password_hash" not in user for user in listed)
    operator = TestClient(client.app, base_url="https://testserver")
    assert operator.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    assert operator.get("/api/users").status_code == 403
    updated = {"role": "label_print", "access": "bgd", "active": True,
               "password": "updated-test-password", "pjt_code": "PJT-A"}
    assert client.put(f"/api/users/{email}", json=updated).status_code == 200
    assert operator.get("/api/auth/session").json()["authenticated"] is False
    assert operator.post("/api/auth/login", json={"email": email, "password": password}).status_code == 401
    assert operator.post("/api/auth/login", json={"email": email, "password": updated["password"]}).status_code == 403
    assert operator.post("/api/auth/web-login", json={"email": email, "password": updated["password"]}).json()["user"]["access"] == "bgd"
    assert client.put(f"/api/users/{email}", json={**updated, "active": False}).status_code == 200
    assert operator.get("/api/auth/session").json()["authenticated"] is False
    operator.close()
    assert client.put(f"/api/users/{settings.admin_email}", json=updated).status_code == 404


def test_login_limit_shared_by_web_and_mobile(client, monkeypatch):
    from app import login_rate_limit

    now = 1_000
    monkeypatch.setattr(login_rate_limit, "monotonic", lambda: now)
    for index in range(login_rate_limit.LoginRateLimiter.ACCOUNT_LIMIT):
        path = "/api/auth/login" if index % 2 == 0 else "/api/auth/web-login"
        assert client.post(path, json={"email": "unknown@example.com", "password": "wrong"}).status_code == 401

    for path in ("/api/auth/login", "/api/auth/web-login"):
        blocked = client.post(path, json={"email": " UNKNOWN@example.com ", "password": "wrong"})
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "300"

    now += 300
    assert client.post("/api/auth/login", json={"email": "unknown@example.com", "password": "wrong"}).status_code == 401


def test_login_ip_limit_blocks_multiple_accounts(client):
    from app.login_rate_limit import LoginRateLimiter

    for index in range(LoginRateLimiter.IP_LIMIT):
        assert client.post("/api/auth/login", json={"email": f"missing{index}@example.com", "password": "wrong"}).status_code == 401
    blocked = client.post("/api/auth/web-login", json={"email": "bgd@paratekno.com", "password": "admin123"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
