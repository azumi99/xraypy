"""SQLite + numpy-brute-force IdentityStore.

Dev/test/small-volume fallback behind the same IdentityStore interface.
Single instance only: idx assignment + AWB pairing are guarded by a process
lock instead of Postgres advisory locks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..config import Settings
from ..warehouse import warehouse_sql
from .shape_filter import ShapeFeatures
from .store import (
    AwbRecord,
    AwbRequiredError,
    IdentityRecord,
    IdentityStore,
    SimilarMatch,
    filter_candidates_by_shape,
    scope_window,
)

logger = logging.getLogger(__name__)


class _WarehouseConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        return self._conn.execute(warehouse_sql(sql), *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS parcel_identity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    track_id INTEGER NOT NULL,
    idx INTEGER,
    status TEXT NOT NULL,
    duplicate_of INTEGER,
    sim_score REAL,
    review_reason TEXT,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    area REAL,
    aspect_ratio REAL,
    hu_moments TEXT,
    crop_ref TEXT,
    crop_ref_side TEXT,
    awb_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, track_id)
);
CREATE TABLE IF NOT EXISTS parcel_embedding (
    identity_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    orientation INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    UNIQUE (identity_id, orientation)
);
CREATE TABLE IF NOT EXISTS review_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT,
    sim_score REAL,
    model_version TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS awb (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    awb_number TEXT NOT NULL,
    hawb_number TEXT,
    part INTEGER,
    total INTEGER,
    label_photo_ref TEXT,
    timecreated TEXT,
    batch_id TEXT,
    status TEXT NOT NULL DEFAULT 'WAITING',
    parcel_id INTEGER,
    paired_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE TABLE IF NOT EXISTS label_print (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mawb TEXT NOT NULL, pcs_mawb INTEGER, hawb TEXT NOT NULL, pcs_hawb INTEGER,
    part INTEGER NOT NULL, airlines_name TEXT, flight_no TEXT, route TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (mawb, hawb, part)
);
"""


class SqliteStore(IdentityStore):
    def __init__(self, settings: Settings, path: str | Path | None = None) -> None:
        self._settings = settings
        self._path = str(path or getattr(settings, "sqlite_path", ":memory:"))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._loop_lock = asyncio.Lock()

    # -- plumbing ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "label_fedex" in tables:
                if "label_print" in tables:
                    self._conn.close()
                    self._conn = None
                    raise RuntimeError("Both label_fedex and label_print exist; resolve before migration")
                self._conn.execute("ALTER TABLE label_fedex RENAME TO label_print")
                tables.add("label_print")
            for name in ("awb", "label_print", "parcel_identity", "parcel_embedding", "review_audit", "session"):
                if name in tables and f"{name}_bgd" in tables:
                    self._conn.close()
                    self._conn = None
                    raise RuntimeError(f"Both {name} and {name}_bgd exist; resolve before migration")
            for name in ("awb", "label_print", "parcel_identity", "parcel_embedding", "review_audit", "session"):
                if name in tables:
                    self._conn.execute(f"ALTER TABLE {name} RENAME TO {name}_bgd")
            self._conn.executescript(warehouse_sql(_SCHEMA, "bgd"))
            self._conn.executescript(warehouse_sql(_SCHEMA, "cas"))
        return _WarehouseConnection(self._conn)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._connect)

        # migrate existing dev databases
        def _migrate() -> None:
            with self._lock:
                conn = self._connect()
                raw = conn._conn
                for table in ("parcel_identity_bgd", "parcel_identity_cas"):
                    cols = {r[1] for r in raw.execute(f"PRAGMA table_info({table})")}
                    if "crop_ref_side" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN crop_ref_side TEXT")
                for table in ("awb_bgd", "awb_cas"):
                    cols = {r[1] for r in raw.execute(f"PRAGMA table_info({table})")}
                    if "hawb_number" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN hawb_number TEXT")
                    if "part" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN part INTEGER")
                    if "total" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN total INTEGER")
                    if "label_photo_ref" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN label_photo_ref TEXT")
                    if "timecreated" not in cols:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN timecreated TEXT")
                for table in ("label_print_bgd", "label_print_cas"):
                    columns = {row[1] for row in raw.execute(f"PRAGMA table_info({table})")}
                    if "pjt_code" not in columns:
                        raw.execute(f"ALTER TABLE {table} ADD COLUMN pjt_code TEXT")
                conn.commit()
                with raw:
                    raw.execute("BEGIN IMMEDIATE")
                    for table in ("label_print_bgd", "label_print_cas"):
                        schema = raw.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()[0]
                        updated_schema = re.sub(
                            r",\s*UNIQUE\s*\(\s*mawb\s*,\s*hawb\s*,\s*part\s*\)",
                            "", schema, flags=re.IGNORECASE,
                        )
                        if updated_schema != schema:
                            sequence = raw.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                            raw.execute(f"ALTER TABLE {table} RENAME TO {table}_previous")
                            raw.execute(updated_schema)
                            raw.execute(f"INSERT INTO {table} SELECT * FROM {table}_previous")
                            raw.execute(f"DROP TABLE {table}_previous")
                            if sequence:
                                raw.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                                raw.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, sequence[0]))
                        raw.execute(
                            f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_pjt_unique "
                            f"ON {table}(mawb, hawb, part, COALESCE(pjt_code, ''))"
                        )
                foreign_keys = raw.execute("PRAGMA foreign_keys").fetchone()[0]
                raw.execute("PRAGMA foreign_keys=OFF")
                try:
                    raw.execute("BEGIN IMMEDIATE")
                    for table in ("awb_bgd", "awb_cas"):
                        indexes = list(raw.execute(f'PRAGMA index_list("{table}")'))
                        targets = [i for i in indexes if i[2] and
                                   [r[2] for r in raw.execute(f'PRAGMA index_info("{i[1]}")')] in
                                   (["awb_number"], ["hawb_number"])]
                        if not targets:
                            continue
                        for index in targets:
                            if index[3] == "c":
                                raw.execute('DROP INDEX "' + index[1].replace('"', '""') + '"')
                        if not any(i[3] == "u" for i in targets):
                            continue
                        sql = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                        for column in ("awb_number", "hawb_number"):
                            ident = rf'(?:"{column}"|\[{column}\]|`{column}`|\b{column}\b)'
                            sql = re.sub(rf'({ident}\s+[^,]*?)\s+UNIQUE\b(?:\s+ON\s+CONFLICT\s+\w+)?', r'\1', sql, flags=re.I)
                            sql = re.sub(rf',\s*(?:CONSTRAINT\s+\w+\s+)?UNIQUE\s*\(\s*{ident}\s*\)(?:\s+ON\s+CONFLICT\s+\w+)?', '', sql, flags=re.I)
                        sql = re.sub(r'^(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?(?:"[^"]+"|\[[^]]+\]|`[^`]+`|\w+)', r'\1"_awb_migration"', sql, count=1, flags=re.I)
                        objects = [r[0] for r in raw.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
                        seq = raw.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                        raw.execute(sql)
                        columns = [r[1] for r in raw.execute(f'PRAGMA table_info("{table}")')]
                        names = ', '.join('"' + c.replace('"', '""') + '"' for c in columns)
                        raw.execute(f'INSERT INTO "_awb_migration" ({names}) SELECT {names} FROM "{table}"')
                        raw.execute(f'DROP TABLE "{table}"')
                        raw.execute(f'ALTER TABLE "_awb_migration" RENAME TO "{table}"')
                        for statement in objects:
                            raw.execute(statement)
                        if seq:
                            raw.execute("INSERT INTO sqlite_sequence(name, seq) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=?)", (table, seq[0], table))
                            raw.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (seq[0], table))
                        for index in raw.execute(f'PRAGMA index_list("{table}")'):
                            if index[2] and [r[2] for r in raw.execute(f'PRAGMA index_info("{index[1]}")')] in (["awb_number"], ["hawb_number"]):
                                raise RuntimeError(f"Could not remove number uniqueness from {table}")
                    if raw.execute("PRAGMA foreign_key_check").fetchone():
                        raise RuntimeError("Foreign key violation after AWB migration")
                    raw.commit()
                except Exception:
                    raw.rollback()
                    raise
                finally:
                    raw.execute(f"PRAGMA foreign_keys={foreign_keys}")

        await asyncio.to_thread(_migrate)
        logger.info("sqlite store ready at %s", self._path)

    async def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _next_identity_id(self) -> int:
        assert self._conn is not None
        return self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM sqlite_sequence "
            "WHERE name IN ('parcel_identity_bgd', 'parcel_identity_cas')"
        ).fetchone()[0]

    def _next_awb_id(self) -> int:
        assert self._conn is not None
        return self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM sqlite_sequence "
            "WHERE name IN ('awb_bgd', 'awb_cas')"
        ).fetchone()[0]

    async def photo_reference_exists(self, ref: str) -> bool:
        def run():
            with self._lock:
                row = self._connect().execute(
                    "SELECT EXISTS (SELECT 1 FROM awb WHERE label_photo_ref = ?) "
                    "OR EXISTS (SELECT 1 FROM parcel_identity WHERE crop_ref = ? OR crop_ref_side = ?) AS found",
                    (ref, ref, ref),
                ).fetchone()
                return bool(row["found"])
        return await asyncio.to_thread(run)

    async def list_photo_references(self) -> list[str]:
        def run():
            with self._lock:
                rows = self._connect().execute(
                    "SELECT label_photo_ref AS ref FROM awb WHERE label_photo_ref IS NOT NULL "
                    "UNION SELECT crop_ref FROM parcel_identity WHERE crop_ref IS NOT NULL "
                    "UNION SELECT crop_ref_side FROM parcel_identity WHERE crop_ref_side IS NOT NULL"
                ).fetchall()
                return [row["ref"] for row in rows]
        return await asyncio.to_thread(run)

    async def photo_recovery_metadata(self, ref: str):
        def run():
            with self._lock:
                rows = self._connect().execute(
                    "SELECT i.id AS identity_id, i.crop_ref, i.crop_ref_side, i.awb_id, "
                    "a.id, a.awb_number, a.batch_id, a.status, a.parcel_id, a.created_at, "
                    "a.hawb_number, a.part, a.total, a.label_photo_ref, a.timecreated "
                    "FROM parcel_identity i LEFT JOIN awb a ON a.id=i.awb_id "
                    "WHERE i.crop_ref=? OR i.crop_ref_side=?", (ref, ref)).fetchall()
                if len(rows) != 1:
                    raise ValueError("photo has ambiguous parcel ownership")
                row = rows[0]
                if (row["crop_ref"] == ref) == (row["crop_ref_side"] == ref):
                    raise ValueError("photo has ambiguous view")
                if row["awb_id"] is not None and (row["id"] is None or row["status"] != "PAIRED" or row["parcel_id"] != row["identity_id"]):
                    raise ValueError("photo has inconsistent AWB ownership")
                return ("top" if row["crop_ref"] == ref else "side", _map_awb_row(row) if row["awb_id"] is not None else None)
        return await asyncio.to_thread(run)

    async def photo_references_existing(self, refs: list[str]) -> set[str]:
        if not refs:
            return set()
        def run():
            placeholders = ",".join("?" for _ in refs)
            with self._lock:
                rows = self._connect().execute(
                    f"SELECT label_photo_ref AS ref FROM awb WHERE label_photo_ref IN ({placeholders}) "
                    f"UNION SELECT crop_ref FROM parcel_identity WHERE crop_ref IN ({placeholders}) "
                    f"UNION SELECT crop_ref_side FROM parcel_identity WHERE crop_ref_side IN ({placeholders})",
                    (*refs, *refs, *refs),
                ).fetchall()
                return {row["ref"] for row in rows}
        return await asyncio.to_thread(run)

    async def list_labels(self, pjt_code=None):
        def run():
            with self._lock:
                conn = self._connect()
                if pjt_code is not None:
                    return [dict(row) for row in conn.execute(
                        "SELECT * FROM label_print WHERE pjt_code=? ORDER BY created_at DESC, id DESC", (pjt_code,)
                    )]
                return [dict(row) for row in conn.execute("SELECT * FROM label_print ORDER BY created_at DESC, id DESC")]
        return await asyncio.to_thread(run)

    async def label_mawbs(self, hawb_number: str) -> list[str]:
        def run():
            with self._lock:
                return [row[0] for row in self._connect().execute(
                    "SELECT DISTINCT mawb FROM label_print WHERE hawb=? ORDER BY mawb", (hawb_number,)
                )]
        return await asyncio.to_thread(run)

    async def import_labels(self, rows, pjt_code=None):
        def run():
            inserted = updated = 0
            with self._lock:
                conn = self._connect()
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for row in rows:
                        exists = conn.execute(
                            "SELECT 1 FROM label_print WHERE mawb=? AND hawb=? AND part=? AND pjt_code IS ?",
                            (row[0], row[2], row[4], pjt_code),
                        ).fetchone()
                        conn.execute("""INSERT INTO label_print
                            (mawb, pcs_mawb, hawb, pcs_hawb, part, airlines_name, flight_no, route, pjt_code)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (mawb, hawb, part, COALESCE(pjt_code, '')) DO UPDATE SET
                            pcs_mawb=excluded.pcs_mawb, pcs_hawb=excluded.pcs_hawb,
                            airlines_name=excluded.airlines_name, flight_no=excluded.flight_no,
                            route=excluded.route, updated_at=CURRENT_TIMESTAMP""", (*row, pjt_code))
                        if exists:
                            updated += 1
                        else:
                            inserted += 1
            return {"imported": inserted, "updated": updated, "skipped": updated}
        return await asyncio.to_thread(run)

    async def delete_labels(self, ids):
        def run():
            with self._lock:
                conn = self._connect()
                with conn:
                    return sum(conn.execute("DELETE FROM label_print WHERE id=?", (id_,)).rowcount for id_ in ids)
        return await asyncio.to_thread(run)

    # -- mapping ------------------------------------------------------------
    @staticmethod
    def _map_identity(row: sqlite3.Row, awb_number: str | None = None) -> IdentityRecord:
        return IdentityRecord(
            id=row["id"],
            batch_id=row["batch_id"],
            track_id=row["track_id"],
            status=row["status"],
            idx=row["idx"],
            duplicate_of=row["duplicate_of"],
            sim_score=row["sim_score"],
            review_reason=row["review_reason"],
            model_name=row["model_name"],
            model_version=row["model_version"],
            area=row["area"],
            aspect_ratio=row["aspect_ratio"],
            hu_moments=json.loads(row["hu_moments"]) if row["hu_moments"] else [],
            crop_ref=row["crop_ref"],
            crop_ref_side=row["crop_ref_side"],
            awb_id=row["awb_id"],
            awb_number=awb_number,
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        )

    # -- parcels -------------------------------------------------------------
    async def get_by_track(self, batch_id: str, track_id: int) -> IdentityRecord | None:
        return await asyncio.to_thread(self._get_by_track, batch_id, track_id)

    def _get_by_track(self, batch_id: str, track_id: int) -> IdentityRecord | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM parcel_identity WHERE batch_id=? AND track_id=?", (batch_id, track_id)
            ).fetchone()
            if row is None:
                return None
            awb_number = None
            if row["awb_id"]:
                arow = conn.execute("SELECT awb_number FROM awb WHERE id=?", (row["awb_id"],)).fetchone()
                awb_number = arow["awb_number"] if arow else None
            return self._map_identity(row, awb_number)

    async def get(self, identity_id: int) -> IdentityRecord | None:
        return await asyncio.to_thread(self._get, identity_id)

    async def get_many_metadata(self, identity_ids: list[int]) -> dict[int, IdentityRecord]:
        if not identity_ids:
            return {}

        def run():
            with self._lock:
                rows = self._connect().execute(
                    "SELECT id, batch_id, track_id, status, idx, sim_score, crop_ref, crop_ref_side, created_at "
                    "FROM parcel_identity WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(identity_ids),),
                ).fetchall()
            return {row["id"]: IdentityRecord(
                id=row["id"], batch_id=row["batch_id"], track_id=row["track_id"],
                status=row["status"], idx=row["idx"], sim_score=row["sim_score"],
                crop_ref=row["crop_ref"], crop_ref_side=row["crop_ref_side"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            ) for row in rows}

        return await asyncio.to_thread(run)

    def _get(self, identity_id: int) -> IdentityRecord | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM parcel_identity WHERE id=?", (identity_id,)).fetchone()
            if row is None:
                return None
            awb_number = None
            if row["awb_id"]:
                arow = conn.execute("SELECT awb_number FROM awb WHERE id=?", (row["awb_id"],)).fetchone()
                awb_number = arow["awb_number"] if arow else None
            return self._map_identity(row, awb_number)

    def _find_similar_sync(
        self,
        embedding: np.ndarray,
        *,
        model_name: str,
        model_version: str,
        batch_id: str,
        exclude_id: int | None,
        shape: ShapeFeatures | None,
    ) -> list[SimilarMatch]:
        cfg = self._settings.dedup
        batch_filter, since = scope_window(cfg, batch_id)
        with self._lock:
            conn = self._connect()
            query = (
                "SELECT p.*, e.orientation, e.embedding AS emb FROM parcel_identity p "
                "JOIN parcel_embedding e ON e.identity_id = p.id "
                "WHERE e.model_name=? AND e.model_version=?"
            )
            params: list = [model_name, model_version]
            if batch_filter is not None:
                query += " AND p.batch_id=?"
                params.append(batch_filter)
            if since is not None:
                query += " AND p.created_at>=?"
                params.append(since.isoformat())
            if exclude_id is not None:
                query += " AND p.id<>?"
                params.append(exclude_id)
            rows = conn.execute(query, params).fetchall()
        matches: dict[int, SimilarMatch] = {}
        for row in rows:
            if row["emb"] is None:
                continue
            emb = np.frombuffer(row["emb"], dtype=np.float32)
            denom = float(np.linalg.norm(emb) * np.linalg.norm(embedding))
            sim = float(emb @ embedding / denom) if denom > 0 else 0.0
            rec = self._map_identity({k: row[k] for k in row.keys() if k not in ("orientation", "emb")})
            if rec.id not in matches or sim > matches[rec.id].similarity:
                matches[rec.id] = SimilarMatch(rec, sim, row["orientation"])
        kept = filter_candidates_by_shape(list(matches.values()), shape, cfg)
        kept.sort(key=lambda m: m.similarity, reverse=True)
        return kept[: cfg.top_k]

    async def find_similar(
        self,
        embedding: np.ndarray,
        *,
        model_name: str,
        model_version: str,
        batch_id: str,
        exclude_id: int | None = None,
        shape: ShapeFeatures | None = None,
    ) -> list[SimilarMatch]:
        return await asyncio.to_thread(
            self._find_similar_sync,
            embedding,
            model_name=model_name,
            model_version=model_version,
            batch_id=batch_id,
            exclude_id=exclude_id,
            shape=shape,
        )

    async def insert_identity(
        self,
        *,
        batch_id: str,
        track_id: int,
        status: str,
        embedding: np.ndarray | None,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim_score: float | None = None,
        duplicate_of: int | None = None,
        review_reason: str | None = None,
    ) -> IdentityRecord:
        return await asyncio.to_thread(
            self._insert_identity_sync,
            batch_id=batch_id,
            track_id=track_id,
            status=status,
            embedding=embedding,
            model_name=model_name,
            model_version=model_version,
            shape=shape,
            crop_ref=crop_ref,
            sim_score=sim_score,
            duplicate_of=duplicate_of,
            review_reason=review_reason,
        )

    def _insert_identity_sync(
        self,
        *,
        batch_id: str,
        track_id: int,
        status: str,
        embedding: np.ndarray | None,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim_score: float | None,
        duplicate_of: int | None,
        review_reason: str | None,
    ) -> IdentityRecord:
        with self._lock:
            conn = self._connect()
            if duplicate_of is not None and self._get(duplicate_of) is None:
                raise ValueError("duplicate target not found in warehouse")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """
                    INSERT INTO parcel_identity
                        (id, batch_id, track_id, status, duplicate_of, sim_score, review_reason,
                         model_name, model_version, area, aspect_ratio, hu_moments, crop_ref, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self._next_identity_id(), batch_id,
                        track_id,
                        status,
                        duplicate_of,
                        sim_score,
                        review_reason,
                        model_name,
                        model_version,
                        shape.area,
                        shape.aspect_ratio,
                        json.dumps(shape.hu),
                        crop_ref,
                        self._now(),
                    ),
                )
                identity_id = cur.lastrowid
                if embedding is not None:
                    conn.execute(
                        "INSERT INTO parcel_embedding (identity_id, model_name, model_version, orientation, embedding)"
                        " VALUES (?,?,?,0,?)",
                        (identity_id, model_name, model_version, embedding.astype(np.float32).tobytes()),
                    )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise DuplicateIdentityError(
                    f"identity (batch={batch_id}, track={track_id}) already exists"
                ) from exc
            except Exception:
                conn.rollback()
                raise
        rec = self._get(identity_id)
        assert rec is not None
        return rec

    def _next_idx_sync(self, conn: sqlite3.Connection, batch_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 AS nxt FROM parcel_identity WHERE batch_id=? AND idx IS NOT NULL",
            (batch_id,),
        ).fetchone()
        return int(row["nxt"])

    def _pair_next_awb_sync(
        self, conn: sqlite3.Connection, parcel_id: int, batch_id: str
    ) -> AwbRecord | None:
        row = conn.execute(
            "SELECT id FROM awb WHERE status='WAITING' AND (batch_id IS NULL OR batch_id=?)"
            " ORDER BY created_at, id LIMIT 1",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE awb SET status='PAIRED', parcel_id=?, paired_at=? WHERE id=?",
            (parcel_id, self._now(), row["id"]),
        )
        conn.execute("UPDATE parcel_identity SET awb_id=? WHERE id=?", (row["id"], parcel_id))
        arow = conn.execute("SELECT * FROM awb WHERE id=?", (row["id"],)).fetchone()
        return AwbRecord(
            id=arow["id"],
            awb_number=arow["awb_number"],
            batch_id=arow["batch_id"],
            status=arow["status"],
            parcel_id=arow["parcel_id"],
            created_at=arow["created_at"],
            hawb_number=arow["hawb_number"],
            part=arow["part"],
            total=arow["total"],
            label_photo_ref=arow["label_photo_ref"],
            timecreated=arow["timecreated"],
        )

    def _finalize_new_sync(self, **kw) -> tuple[IdentityRecord, AwbRecord | None]:
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM parcel_identity WHERE batch_id=? AND track_id=?",
                (kw["batch_id"], kw["track_id"]),
            ).fetchone()
            if existing:
                conn.commit()
                return self._get(existing["id"]), None  # idempotent re-delivery
            idx = self._next_idx_sync(conn, kw["batch_id"])
            try:
                cur = conn.execute(
                    """
                    INSERT INTO parcel_identity
                        (id, batch_id, track_id, idx, status, sim_score, model_name, model_version,
                         area, aspect_ratio, hu_moments, crop_ref, created_at, crop_ref_side)
                    VALUES (?,?,?,?,'NEW',?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self._next_identity_id(), kw["batch_id"],
                        kw["track_id"],
                        idx,
                        kw["sim_score"],
                        kw["model_name"],
                        kw["model_version"],
                        kw["shape"].area,
                        kw["shape"].aspect_ratio,
                        json.dumps(kw["shape"].hu),
                        kw["crop_ref"],
                        self._now(),
                        kw.get("crop_ref_side"),
                    ),
                )
                identity_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO parcel_embedding (identity_id, model_name, model_version, orientation, embedding)"
                    " VALUES (?,?,?,0,?)",
                    (
                        identity_id,
                        kw["model_name"],
                        kw["model_version"],
                        kw["embedding"].astype(np.float32).tobytes(),
                    ),
                )
                awb = self._pair_next_awb_sync(conn, identity_id, kw["batch_id"])
                if kw["require_awb"] and awb is None:
                    raise AwbRequiredError("no WAITING AWB available for NEW parcel")
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise DuplicateIdentityError(
                    f"identity (batch={kw['batch_id']}, track={kw['track_id']}) already exists"
                ) from exc
            except Exception:
                conn.rollback()
                raise
        rec = self._get(identity_id)
        assert rec is not None
        return rec, awb

    async def finalize_new(self, **kw) -> tuple[IdentityRecord, AwbRecord | None]:
        async with self._loop_lock:
            return await asyncio.to_thread(self._finalize_new_sync, **kw)

    def _promote_sync(self, identity_id: int, require_awb: bool) -> tuple[IdentityRecord, AwbRecord | None]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT batch_id FROM parcel_identity WHERE id=?", (identity_id,)).fetchone()
            if row is None:
                raise KeyError(f"identity {identity_id} not found")
            idx = self._next_idx_sync(conn, row["batch_id"])
            conn.execute(
                "UPDATE parcel_identity SET status='NEW', idx=?, review_reason=NULL WHERE id=?",
                (idx, identity_id),
            )
            awb = self._pair_next_awb_sync(conn, identity_id, row["batch_id"])
            if require_awb and awb is None:
                conn.rollback()
                raise AwbRequiredError("no WAITING AWB available for promoted parcel")
            conn.execute(
                "INSERT INTO review_audit (identity_id, action, operator, reason, model_version, at)"
                " VALUES (?,?,?,?,?,?)",
                (identity_id, "NOT_DUPLICATE", "operator", "resolved to NEW", None, self._now()),
            )
            conn.commit()
        rec = self._get(identity_id)
        assert rec is not None
        return rec, awb

    async def promote_to_new(
        self, identity_id: int, *, require_awb: bool
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        async with self._loop_lock:
            return await asyncio.to_thread(self._promote_sync, identity_id, require_awb)

    async def resolve(
        self, identity_id: int, action: str, operator: str, reason: str | None
    ) -> IdentityRecord | None:
        if action not in ("CONFIRM_DUPLICATE", "RESCAN_INTENTIONAL"):
            raise ValueError(f"invalid resolve action: {action!r}")
        target = await self.confirm_duplicate_target(identity_id)
        await asyncio.to_thread(
            self._resolve_sync, identity_id, action, operator, reason, target.id if target else None
        )
        return await self.get(identity_id)

    def _resolve_sync(
        self, identity_id: int, action: str, operator: str, reason: str | None, duplicate_of: int | None
    ) -> None:
        with self._lock:
            conn = self._connect()
            if self._get(identity_id) is None:
                return
            conn.execute(
                "UPDATE parcel_identity SET status='DUPLICATE', duplicate_of=COALESCE(duplicate_of, ?) WHERE id=?",
                (duplicate_of, identity_id),
            )
            conn.execute(
                "INSERT INTO review_audit (identity_id, action, operator, reason, at) VALUES (?,?,?,?,?)",
                (identity_id, action, operator, reason, self._now()),
            )
            conn.commit()

    def _pending_reviews_sync(self) -> list[IdentityRecord]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT id FROM parcel_identity WHERE status='REVIEW' ORDER BY created_at"
            ).fetchall()
        return [r for r in (self._get(row["id"]) for row in rows) if r is not None]

    async def pending_reviews(self) -> list[IdentityRecord]:
        return await asyncio.to_thread(self._pending_reviews_sync)

    async def confirm_duplicate_target(self, identity_id: int) -> IdentityRecord | None:
        rec = await self.get(identity_id)
        if rec is None or rec.duplicate_of is None:
            return None
        return await self.get(rec.duplicate_of)

    async def add_side_capture(
        self,
        identity_id: int,
        embedding: np.ndarray | None,
        crop_ref: str,
        *,
        model_name: str,
        model_version: str,
    ) -> None:
        def _run() -> None:
            with self._lock:
                conn = self._connect()
                if self._get(identity_id) is None:
                    return
                conn.execute("UPDATE parcel_identity SET crop_ref_side=? WHERE id=?", (crop_ref, identity_id))
                if embedding is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO parcel_embedding (identity_id, model_name, model_version, orientation, embedding)"
                        " VALUES (?,?,?,?,?)",
                        (identity_id, model_name, model_version, 1, embedding.astype(np.float32).tobytes()),
                    )
                conn.commit()

        await asyncio.to_thread(_run)

    async def list_audit(self, identity_id: int) -> list[dict]:
        def _run() -> list[dict]:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT at, action, operator, reason, sim_score FROM review_audit"
                    " WHERE identity_id=? ORDER BY at DESC",
                    (identity_id,),
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_run)

    async def insert_audit(
        self,
        identity_id: int,
        action: str,
        operator: str,
        reason: str | None,
        sim_score: float | None,
        model_version: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._insert_audit_sync, identity_id, action, operator, reason, sim_score, model_version
        )

    def _insert_audit_sync(self, identity_id, action, operator, reason, sim_score, model_version) -> None:
        with self._lock:
            conn = self._connect()
            if self._get(identity_id) is None:
                return
            conn.execute(
                "INSERT INTO review_audit (identity_id, action, operator, reason, sim_score, model_version, at)"
                " VALUES (?,?,?,?,?,?,?)",
                (identity_id, action, operator, reason, sim_score, model_version, self._now()),
            )
            conn.commit()

    async def last_idx(self, batch_id: str) -> int:
        def _run() -> int:
            with self._lock:
                conn = self._connect()
                row = conn.execute(
                    "SELECT COALESCE(MAX(idx), -1) AS m FROM parcel_identity WHERE batch_id=? AND idx IS NOT NULL",
                    (batch_id,),
                ).fetchone()
                return int(row["m"])

        return await asyncio.to_thread(_run)

    # -- AWBs ------------------------------------------------------------------
    async def insert_awb(
        self,
        awb_number: str,
        batch_id: str | None = None,
        hawb_number: str | None = None,
        part: int | None = None,
        total: int | None = None,
        *,
        qr_scanned: bool = False,
    ) -> AwbRecord:
        return await asyncio.to_thread(self._insert_awb_sync, awb_number, batch_id, hawb_number, part, total, qr_scanned)

    def _insert_awb_sync(
        self,
        awb_number: str,
        batch_id: str | None,
        hawb_number: str | None,
        part: int | None,
        total: int | None,
        qr_scanned: bool,
    ) -> AwbRecord:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = self._now()
                cursor = conn.execute(
                    "INSERT INTO awb (id, awb_number, batch_id, hawb_number, part, total, created_at, timecreated) VALUES (?,?,?,?,?,?,?,?)",
                    (self._next_awb_id(), awb_number, batch_id, hawb_number, part, total, now, now if qr_scanned else None),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            row = conn.execute("SELECT * FROM awb WHERE id=?", (cursor.lastrowid,)).fetchone()
        return _map_awb_row(row)

    def _list_awbs_sync(self, status: str | None) -> list[AwbRecord]:
        with self._lock:
            conn = self._connect()
            if status:
                rows = conn.execute(
                    "SELECT * FROM awb WHERE status=? ORDER BY created_at", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM awb ORDER BY created_at").fetchall()
        return [_map_awb_row(r) for r in rows]

    async def list_awbs(self, status: str | None = None) -> list[AwbRecord]:
        return await asyncio.to_thread(self._list_awbs_sync, status)

    async def attach_label_photo(self, awb_id: int, ref: str) -> bool:
        def update():
            with self._lock:
                conn = self._connect()
                try:
                    result = conn.execute("UPDATE awb SET label_photo_ref=? WHERE id=? AND label_photo_ref IS NULL", (ref, awb_id))
                    conn.commit()
                    return result.rowcount == 1
                except BaseException:
                    conn.rollback()
                    raise
        return await asyncio.to_thread(update)

    async def delete_awb(self, awb_id: int, *, force: bool = False) -> bool:
        def _run() -> bool:
            with self._lock:
                conn = self._connect()
                awb = conn.execute("SELECT awb_number, status FROM awb WHERE id=?", (awb_id,)).fetchone()
                if awb is None:
                    return False
                if awb["status"] == "PAIRED":
                    if not force:
                        return False
                    unlinked = conn.execute(
                        "SELECT id FROM parcel_identity WHERE awb_id=?", (awb_id,)
                    ).fetchall()
                    for row in unlinked:
                        conn.execute("UPDATE parcel_identity SET awb_id=NULL WHERE id=?", (row["id"],))
                        conn.execute(
                            "INSERT INTO review_audit (identity_id, action, operator, reason, at)"
                            " VALUES (?, 'AWB_FORCE_UNLINK', 'operator', ?, ?)",
                            (
                                row["id"],
                                f"AWB {awb['awb_number']} force-deleted; parcel unlinked",
                                self._now(),
                            ),
                        )
                conn.execute("DELETE FROM awb WHERE id=?", (awb_id,))
                conn.commit()
                return True

        return await asyncio.to_thread(_run)


def _map_awb_row(row) -> AwbRecord:
    return AwbRecord(
        id=row["id"],
        awb_number=row["awb_number"],
        label_photo_ref=row["label_photo_ref"],
        timecreated=row["timecreated"],
        batch_id=row["batch_id"],
        status=row["status"],
        parcel_id=row["parcel_id"],
        created_at=row["created_at"],
        hawb_number=row["hawb_number"],
        part=row["part"],
        total=row["total"],
    )


class DuplicateIdentityError(RuntimeError):
    pass
