"""Durable outbox for finalized, database-referenced photos."""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import stat
import struct
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .photo_obs import ObsPhotoStore, object_key

logger = logging.getLogger(__name__)
_FINAL_CHUNK = b"xray-cargo-final\x001"
_PENDING_CHUNK = b"xray-cargo-pending\x001"


def _mark_png(data: bytes, marker: bytes) -> bytes:
    """Add an internal finalization marker without altering image pixels."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[-12:-4] != b"\x00\x00\x00\x00IEND":
        raise ValueError("invalid PNG")
    payload = b"tEXt" + marker
    chunk = struct.pack(">I", len(marker)) + payload + struct.pack(">I", zlib.crc32(payload))
    return data[:-12] + chunk + data[-12:]


def mark_final_png(data: bytes) -> bytes:
    return _mark_png(data, _FINAL_CHUNK)


def mark_pending_png(data: bytes) -> bytes:
    return _mark_png(data, _PENDING_CHUNK)


def _has_marker(data: bytes, marker: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + size
        if end > len(data):
            return False
        kind, value = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        if kind == b"tEXt" and value == marker:
            return struct.unpack_from(">I", data, offset + 8 + size)[0] == zlib.crc32(kind + value)
        if kind == b"IEND":
            return False
        offset = end
    return False


def is_final_png(data: bytes) -> bool:
    return _has_marker(data, _FINAL_CHUNK)


def is_pending_png(data: bytes) -> bool:
    return _has_marker(data, _PENDING_CHUNK) and not is_final_png(data)

class PhotoSync:
    def __init__(self, settings, store=None):
        self.enabled = getattr(settings, "photo_sync_enabled", False)
        endpoint = getattr(settings, "photo_obs_endpoint", "")
        self.obs = ObsPhotoStore(settings) if endpoint else None
        self._obs_keys = {}
        self._obs_keys_locks = {site: threading.Lock() for site in ("bgd", "cas")}
        if not self.enabled:
            return
        self.store = store
        self.root = Path(settings.crop_dir)
        self.outbox = str(settings.photo_sync_outbox_path)
        self.tokens = {
            "bgd": settings.photo_sync_bgd_token,
            "cas": settings.photo_sync_cas_token,
        }
        if self.obs is None:
            url = urlsplit(settings.photo_sync_url)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.query or url.fragment):
                raise ValueError("photo sync URL must be an HTTPS base URL")
            if not all(self.tokens.values()):
                raise ValueError("photo sync requires a token for each site")
            self.url = settings.photo_sync_url.rstrip("/") + "/api/sync/photos"
        else:
            # Acknowledgments for the old VM are independent of acknowledgments for OBS.
            destination = hashlib.sha256(f"{endpoint.rstrip('/')}/{self.obs.bucket}".encode()).hexdigest()[:12]
            self.outbox += f".obs-{destination}"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS photo_outbox (
                site TEXT NOT NULL, ref TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                next_attempt REAL NOT NULL, lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (site, ref))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(photo_outbox)")}
            if "lease_token" not in columns:
                db.execute("ALTER TABLE photo_outbox ADD COLUMN lease_token TEXT")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE photo_outbox ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.outbox, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _parts(site, ref):
        if site not in ("bgd", "cas") or not isinstance(ref, str):
            raise ValueError("invalid site or photo reference")
        parts = ref.split("/")
        if (len(parts) < 2 or any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts)
                or (site == "cas" and parts[0] != "cas")
                or (site == "bgd" and parts[0].casefold() == "cas")):
            raise ValueError("invalid site or photo reference")
        return parts

    def _read(self, site, ref):
        parts = self._parts(site, ref)
        if os.name == "nt":
            # ponytail: resolve+is_relative_to menggantikan O_NOFOLLOW di Windows;
            # cek symlink pada path asli SEBELUM resolve (resolve mengikuti symlink).
            original = self.root.joinpath(*parts)
            if original.is_symlink():
                raise ValueError("unsafe photo path")
            path = original.resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError("unsafe photo path")
            if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError("photo is not a regular file or exceeds upload size limit")
            return path.read_bytes()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as photo:
                info = os.fstat(photo.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("photo is not a regular file")
                if info.st_size > 12 * 1024 * 1024:
                    raise ValueError("photo exceeds upload size limit")
                return photo.read(12 * 1024 * 1024 + 1)
        finally:
            os.close(directory)

    def enqueue(self, site, ref, require_final=False):
        if not self.enabled:
            return
        data = self._read(site, ref)
        if require_final and not is_final_png(data):
            raise ValueError("photo is not finalized")
        digest = hashlib.sha256(data).hexdigest()
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO photo_outbox (site,ref,sha256,size,status,attempts,next_attempt) VALUES (?, ?, ?, ?, 'pending', 0, 0)",
                       (site, ref, digest, len(data)))
            row = db.execute("SELECT sha256, size FROM photo_outbox WHERE site=? AND ref=?",
                             (site, ref)).fetchone()
            if row != (digest, len(data)):
                raise ValueError("photo reference already has different contents")

    def status(self, site, refs):
        if not self.enabled:
            return {}
        if site not in self.tokens:
            raise ValueError("invalid site")
        result = {}
        unregistered = {}
        with self._db() as db:
            for ref in refs:
                parts = self._parts(site, ref)
                row = db.execute("SELECT status FROM photo_outbox WHERE site=? AND ref=?",
                                 (site, ref)).fetchone()
                if row:
                    result[ref] = row[0]
                else:
                    unregistered[ref] = parts
        keys = self._listed_obs_keys(site) if self.obs is not None and unregistered else set()
        for ref, parts in unregistered.items():
            if object_key(site, parts) in keys:
                result[ref] = "synced"
                continue
            try:
                data = self._read(site, ref)
                is_label = ref.startswith("cas/label-photos/" if site == "cas" else "label-photos/")
                result[ref] = "unknown" if is_label or is_final_png(data) else "failed"
            except (OSError, ValueError):
                result[ref] = "unknown"
        return result

    def _listed_obs_keys(self, site):
        with self._obs_keys_locks[site]:
            expires, keys = self._obs_keys.get(site, (0, set()))
            if time.monotonic() >= expires:
                try:
                    keys = self.obs.list_photo_keys(site)
                except OSError:
                    logger.warning("Could not list OBS photos for %s", site)
                    keys = set()
                    ttl = 10
                else:
                    ttl = 60
                self._obs_keys[site] = (time.monotonic() + ttl, keys)
            return keys

    def _recover_pending(self, site, ref, view, awb):
        import cv2
        import numpy as np

        from .pipeline import _save_capture

        parts = self._parts(site, ref)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as photo:
                info = os.fstat(photo.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 12 * 1024 * 1024:
                    raise ValueError("invalid pending photo")
                data = photo.read(12 * 1024 * 1024 + 1)
            if is_final_png(data):
                return
            if not is_pending_png(data):
                raise ValueError("photo is not pending")
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if image is None or len(image.shape) != 3 or image.shape[2] != 4:
                raise ValueError("invalid pending image")
            _save_capture(Path(parts[-1]), image, view, awb,
                          owned=(info.st_dev, info.st_ino), dir_fd=directory)
        finally:
            os.close(directory)

    async def reconcile(self):
        """Recover DB-referenced photos finalized before an interrupted enqueue."""
        if not self.enabled or self.store is None:
            return
        from .warehouse import warehouse_context

        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                refs = await self.store.list_photo_references()
            finally:
                warehouse_context.reset(token)
            with self._db() as db:
                known = {row[0] for row in db.execute("SELECT ref FROM photo_outbox WHERE site=?", (site,))}
            for ref in refs:
                if ref in known:
                    continue
                try:
                    label_prefix = "cas/label-photos/" if site == "cas" else "label-photos/"
                    is_label = ref.startswith(label_prefix) and ref.lower().endswith((".jpg", ".jpeg", ".png"))
                    if not is_label:
                        data = await asyncio.to_thread(self._read, site, ref)
                        if is_pending_png(data):
                            token = warehouse_context.set(site)
                            try:
                                view, awb = await self.store.photo_recovery_metadata(ref)
                            finally:
                                warehouse_context.reset(token)
                            await asyncio.to_thread(self._recover_pending, site, ref, view, awb)
                    await asyncio.to_thread(self.enqueue, site, ref, not is_label)
                except (OSError, ValueError, NotImplementedError):
                    logger.warning("photo sync recovery skipped an inaccessible or invalid photo")

    async def verify_legacy(self, client):
        """Acknowledge legacy unmarked photos whose VM copy is byte-identical.

        Foto pra-marker tidak diunggah ulang; kecocokan digest terhadap server
        adalah bukti cukup untuk menandai synced tanpa mengubah byte lokal.
        """
        if not self.enabled or self.store is None:
            return
        from .warehouse import warehouse_context

        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                refs = await self.store.list_photo_references()
            finally:
                warehouse_context.reset(token)
            label_prefix = "cas/label-photos/" if site == "cas" else "label-photos/"
            verified = 0
            for ref in refs:
                if ref.startswith(label_prefix):
                    continue
                with self._db() as db:
                    row = db.execute("SELECT status FROM photo_outbox WHERE site=? AND ref=?", (site, ref)).fetchone()
                if row is not None:
                    continue
                try:
                    data = await asyncio.to_thread(self._read, site, ref)
                except (OSError, ValueError):
                    continue
                if is_final_png(data) or is_pending_png(data):
                    continue
                digest = hashlib.sha256(data).hexdigest()
                headers = {"Authorization": "Bearer " + self.tokens[site]}
                try:
                    response = await client.get(self.url, params={"ref": ref, "digest": "1"}, headers=headers)
                except httpx.HTTPError:
                    continue
                if response.status_code != 200:
                    continue
                remote = response.json().get("sha256") if response.headers.get("content-type", "").startswith("application/json") else None
                if remote and hmac.compare_digest(remote, digest):
                    stat = (self.root / ref).stat()
                    with self._db() as db:
                        db.execute("INSERT OR IGNORE INTO photo_outbox (site,ref,sha256,size,status,attempts,next_attempt) VALUES (?,?,?,?,'synced',0,0)",
                                   (site, ref, digest, stat.st_size))
                    verified += 1
            if verified:
                logger.info("photo sync legacy verified %d identical %s photos", verified, site)

    async def run(self):
        if not self.enabled:
            return
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            next_reconcile = 0.0
            next_legacy = 0.0
            while True:
                if self.store is not None and time.monotonic() >= next_reconcile:
                    await self.reconcile()
                    next_reconcile = time.monotonic() + 60
                if self.obs is None and self.store is not None and time.monotonic() >= next_legacy:
                    await self.verify_legacy(client)
                    next_legacy = time.monotonic() + 300
                lease = secrets.token_hex(16)
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    row = db.execute("""SELECT site, ref, sha256, size, attempts FROM photo_outbox
                        WHERE status='pending' AND next_attempt<=? AND lease_until<=?
                        ORDER BY next_attempt, rowid LIMIT 1""", (now, now)).fetchone()
                    if row:
                        db.execute("UPDATE photo_outbox SET lease_token=?, lease_until=? WHERE site=? AND ref=?",
                                   (lease, now + 120, row[0], row[1]))
                if row is None:
                    await asyncio.sleep(1)
                    continue
                site, ref, digest, size, attempts = row
                try:
                    data = await asyncio.to_thread(self._read, site, ref)
                except OSError:
                    state = "pending"
                except ValueError:
                    state = "failed"
                else:
                    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                        state = "failed"
                    else:
                        if self.obs is not None:
                            key = object_key(site, self._parts(site, ref))
                            try:
                                state = await asyncio.to_thread(self.obs.upload, key, data, digest)
                            except Exception as exc:
                                logger.warning("OBS photo sync will retry (%s)", type(exc).__name__)
                                state = "pending"
                        else:
                            try:
                                response = await client.put(
                                    self.url, headers={"Authorization": f"Bearer {self.tokens[site]}"},
                                    data={"ref": ref, "sha256": digest},
                                    files={"file": (Path(ref).name, data, "application/octet-stream")},
                                )
                                try:
                                    ack = response.json()
                                except ValueError:
                                    ack = None
                                confirmed = (response.status_code == 200 and isinstance(ack, dict)
                                             and ack.get("status") in ("created", "exists") and ack.get("sha256") == digest)
                                state = ("synced" if confirmed else
                                         "pending" if response.status_code in (200, 401, 403, 404, 429) or response.status_code >= 500 else "failed")
                            except httpx.RequestError:
                                state = "pending"
                attempts += 1
                delay = min(300, 2 ** min(attempts - 1, 9)) if state == "pending" else 0
                with self._db() as db:
                    db.execute("""UPDATE photo_outbox SET status=?, attempts=?, next_attempt=?, lease_token=NULL, lease_until=0
                        WHERE site=? AND ref=? AND status='pending' AND lease_token=?""",
                               (state, attempts, time.time() + delay, site, ref, lease))


class PhotoSyncService:
    def __init__(self, settings):
        raise RuntimeError("Photo sync is disabled pending safe photo verification")
