"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth_store import AuthStore, verify_password
from .awb.router import router as awb_router
from .awb.service import AwbService
from .config import Settings, load_settings
from .db_tunnel import db_tunnel
from .dedup.review import router as review_router
from .dedup.service import DedupService
from .dedup.store import IdentityStore, make_store
from .embedding.registry import create_embedder
from .embedding.worker import EmbeddingWorker
from .events import EventBus
from .labels import router as labels_router
from .manifest_tunnels import manifest_tunnels
from .pipeline import Pipeline
from .security import (
    COOKIE,
    REFRESH_AFTER,
    SESSION_SECONDS,
    identity,
    issue_session,
    require_operator,
    same_origin,
    session_claims,
)
from .sources import SourceManager
from .warehouse import warehouse_context

logger = logging.getLogger(__name__)

UI_DIST = Path(__file__).parent.parent / "ui" / "dist"


class Login(BaseModel):
    email: str
    password: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def backend_lifespan(app: FastAPI):
        if not settings.jwt_secret or len(settings.jwt_secret.encode()) < 32:
            raise RuntimeError("CV_JWT_SECRET must contain at least 32 bytes")
        if not settings.bootstrap_password:
            raise RuntimeError("CV_BOOTSTRAP_PASSWORD is required for initial user provisioning")
        # 1. embedder (refuses to start on failure, per spec)
        embedder = create_embedder(settings.embedding)
        worker = EmbeddingWorker(embedder, settings.embedding)
        worker.start()
        # 2. identity store
        store: IdentityStore = make_store(settings)
        await store.initialize()
        auth_store = AuthStore(settings, store)
        await auth_store.initialize()
        app.state.auth_store = auth_store
        # 3. services
        bus = EventBus()
        dedup = DedupService(settings, worker, store)
        awb = AwbService(settings, store)
        source_mgr = SourceManager(settings)
        pipeline = Pipeline(settings, dedup, bus, source_mgr)
        app.state.source_mgr = source_mgr
        # app state
        app.state.settings = settings
        app.state.worker = worker
        app.state.store = store
        app.state.dedup = dedup
        app.state.awb = awb
        app.state.bus = bus
        app.state.pipeline = pipeline
        settings.crop_dir.mkdir(parents=True, exist_ok=True)
        from .photo_sync import PhotoSync
        photo_sync = PhotoSync(settings, store)
        app.state.photo_sync = photo_sync
        dedup.photo_sync = photo_sync
        async def supervise_photo_sync():
            while True:
                try:
                    await photo_sync.run()
                    return
                except Exception:  # noqa: BLE001 — retry after transient outbox errors
                    logger.exception("photo sync worker stopped; retrying")
                    await asyncio.sleep(5)

        sync_task = asyncio.create_task(supervise_photo_sync()) if photo_sync.enabled else None
        logger.info(
            "cv-service ready (model=%s store=%s mode=%s)",
            embedder.name,
            settings.dedup.store,
            settings.pipeline_mode,
        )
        yield
        if sync_task is not None:
            sync_task.cancel()
            await asyncio.gather(sync_task, return_exceptions=True)
        from .warehouse import warehouse_context
        for access, runtime in getattr(app.state, "warehouse_runtimes", {}).items():
            token = warehouse_context.set(access)
            try:
                await runtime.pipeline.stop_session()
                runtime.source_mgr.close()
            finally:
                warehouse_context.reset(token)
        if "bgd" not in getattr(app.state, "warehouse_runtimes", {}):
            await pipeline.stop_session()
            source_mgr.close()
        worker.stop()
        auth_store.close()
        await store.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        async with manifest_tunnels(), db_tunnel(settings), backend_lifespan(app):
            yield

    app = FastAPI(title="cv-service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.revoked_jtis: set[str] = set()

    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.url.path
        if path == "/api/sync/photos" and request.method in ("GET", "PUT"):
            from .photo_receiver import MAX_BYTES, _site
            try:
                _site(settings, request.headers.get("authorization"))
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if request.method == "PUT":
                length = request.headers.get("content-length", "")
                if not length.isascii() or not length.isdigit() or int(length) > MAX_BYTES + 65536:
                    return JSONResponse({"detail": "Request body too large or length missing"}, status_code=413)
            return await call_next(request)
        if path == "/api" or path.startswith("/api/") or path == "/crops" or path.startswith("/crops/"):
            if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
                return JSONResponse({"detail": "invalid origin"}, status_code=403)
            claims = session_claims(request)
            user = {"email": claims["sub"], "access": claims["access"]} if claims else None
            if path not in ("/api/auth/login", "/api/auth/session", "/api/auth/logout") and not user:
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            if user and (path == "/crops" or path.startswith("/crops/")):
                crop = Path(path.removeprefix("/crops/")).parts
                is_cas_path = bool(crop) and crop[0].casefold() == "cas"
                if ".." in crop or (user["access"] == "bgd" and is_cas_path) or (user["access"] == "cas" and crop[:1] != ("cas",)):
                    return JSONResponse({"detail": "forbidden"}, status_code=403)
            if user:
                context_token = warehouse_context.set(user["access"])
                try:
                    response = await call_next(request)
                finally:
                    warehouse_context.reset(context_token)
                if claims["exp"] - time.time() < REFRESH_AFTER:
                    # Sliding refresh: sesi aktif tidak pernah kedaluwarsa;
                    # masa berlaku mutlak tetap 3 bulan per token.
                    token, fresh = issue_session(settings, user)
                    app.state.revoked_jtis.add(claims["jti"])
                    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                                        samesite="strict", secure=request.url.scheme == "https", path="/")
                return response
        return await call_next(request)

    @app.get("/api/auth/session")
    async def auth_session(request: Request):
        user = identity(request)
        return JSONResponse({"authenticated": user is not None, "user": user}, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/login")
    async def login(request: Request, body: Login):
        if not settings.jwt_secret or len(settings.jwt_secret.encode()) < 32:
            return JSONResponse({"detail": "authentication unavailable"}, status_code=503)
        user = await app.state.auth_store.get(body.email.strip().lower())
        if not user or not await asyncio.to_thread(verify_password, body.password, user["password_hash"]):
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        token, _claims = issue_session(settings, user)
        response = JSONResponse({"authenticated": True, "user": {"email": user["email"], "access": user["access"]}}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        claims = session_claims(request)
        if claims:
            app.state.revoked_jtis.add(claims["jti"])
        response = JSONResponse({"authenticated": False, "user": None}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    app.include_router(awb_router, prefix="/api")
    from .settings_router import router as settings_router
    app.include_router(settings_router, prefix="/api")
    from .photo_delivery import router as photo_delivery_router
    from .photo_receiver import router as photo_receiver_router
    app.include_router(photo_receiver_router)
    app.include_router(photo_delivery_router)

    @app.get("/api/sync/status")
    async def photo_sync_status(request: Request, _: Annotated[None, Depends(require_operator)], ref: Annotated[list[str] | None, Query()] = None):
        ref = ref or []
        if len(ref) > 100:
            raise HTTPException(422, "Too many photo references")
        from .warehouse import current_warehouse
        site = current_warehouse()
        sync = request.app.state.photo_sync
        if not sync.enabled:
            from .photo_receiver import _site, photo_stored
            try:
                _site(settings, None)
            except HTTPException as exc:
                if exc.status_code != 401:
                    return {"enabled": False, "items": {}}
            else:
                return {"enabled": False, "items": {}}
            allowed = await request.app.state.store.photo_references_existing(ref)
            def check_stored(item):
                try:
                    return photo_stored(settings.crop_dir, site, item)
                except (OSError, ValueError, AttributeError):
                    return False
            results = await asyncio.gather(*(asyncio.to_thread(check_stored, item) for item in allowed))
            return {"enabled": True, "items": {item: "stored" if stored else "missing" for item, stored in zip(allowed, results, strict=True)}}
        allowed = await request.app.state.store.photo_references_existing(ref)
        return {"enabled": True, "items": await asyncio.to_thread(sync.status, site, allowed)}
    app.include_router(labels_router, prefix="/api")
    app.include_router(review_router, prefix="/api")
    from .photos import router as photos_router

    app.include_router(photos_router, prefix="/api")
    from .session_router import router as session_router
    from .source_router import router as source_router

    app.include_router(session_router, prefix="/api")
    app.include_router(source_router, prefix="/api")
    from .ws.stream import router as ws_router

    app.include_router(ws_router)
    # crop thumbnails (paths come from the DB crop_ref) — MUST be mounted
    # BEFORE the catch-all "/" UI mount, or /crops/* requests never reach it
    app.state.crop_dir = settings.crop_dir
    settings.crop_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/crops", StaticFiles(directory=str(settings.crop_dir)), name="crops")
    # UI: Vite+React build (ui/dist). Build dulu dengan `cd ui && npm run build`.
    # Catch-all: path tanpa ekstensi -> index.html (SPA routing /live /awb /review),
    # path berekstensi -> file asset dari ui/dist.
    UI_DIST.mkdir(parents=True, exist_ok=True)
    index_file = UI_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path and "." in full_path.split("/")[-1]:
            root = UI_DIST.resolve()
            candidate = (root / full_path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return FileResponse(index_file)

    return app


app = create_app()
