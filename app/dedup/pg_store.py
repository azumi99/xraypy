"""pgvector-backed IdentityStore (schema `cv`, asyncpg, parameterized SQL)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import asyncpg
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
    hu_from_json,
    scope_window,
)

logger = logging.getLogger(__name__)


class _WarehouseConnection:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        method = getattr(self._conn, name)
        if name in ("fetch", "fetchrow", "fetchval", "execute"):
            async def run(query, *args, **kwargs):
                return await method(warehouse_sql(query), *args, **kwargs)
            return run
        return method


class PgVectorStore(IdentityStore):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        from ..db import create_pool  # local import avoids circular at module load

        self._pool = await create_pool(self._settings)
        logger.info("pgvector store ready (schema cv)")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def listen_for_awb_waiting_changes(
        self, publish: Callable[[str], None], set_available: Callable[[bool], None],
    ) -> None:
        assert self._pool is not None
        channel = "cv_awb_waiting_changed"

        def on_change(_connection, _pid, _channel, warehouse):
            if warehouse in ("bgd", "cas"):
                publish(warehouse)

        while True:
            try:
                async with self._pool.acquire(timeout=10) as conn:
                    disconnected = asyncio.Event()

                    def on_disconnect(_connection, event=disconnected):
                        event.set()

                    await conn.add_listener(channel, on_change)
                    conn.add_termination_listener(on_disconnect)
                    set_available(True)
                    # Reconcile changes committed while the listener was disconnected.
                    for warehouse in ("bgd", "cas"):
                        publish(warehouse)
                    try:
                        while not disconnected.is_set():
                            try:
                                await asyncio.wait_for(disconnected.wait(), timeout=15)
                            except TimeoutError:
                                await conn.execute("SELECT 1", timeout=5)
                    finally:
                        set_available(False)
                        if not conn.is_closed():
                            conn.remove_termination_listener(on_disconnect)
                            await conn.remove_listener(channel, on_change)
            except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError, RuntimeError):
                logger.warning("AWB notification listener disconnected; retrying")
            await asyncio.sleep(2)

    async def _run(self, query: str, *args):
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetch(warehouse_sql(query), *args)

    async def _runrow(self, query: str, *args):
        rows = await self._run(query, *args)
        return rows[0] if rows else None

    async def list_labels(self):
        return [dict(row) for row in await self._run("SELECT * FROM cv.label_print ORDER BY created_at DESC, id DESC")]

    async def photo_reference_exists(self, ref: str) -> bool:
        row = await self._runrow(
            "SELECT EXISTS (SELECT 1 FROM cv.awb WHERE label_photo_ref = $1) "
            "OR EXISTS (SELECT 1 FROM cv.parcel_identity WHERE crop_ref = $1 OR crop_ref_side = $1) AS found", ref,
        )
        return bool(row["found"])

    async def list_photo_references(self) -> list[str]:
        rows = await self._run(
            "SELECT label_photo_ref AS ref FROM cv.awb WHERE label_photo_ref IS NOT NULL "
            "UNION SELECT crop_ref FROM cv.parcel_identity WHERE crop_ref IS NOT NULL "
            "UNION SELECT crop_ref_side FROM cv.parcel_identity WHERE crop_ref_side IS NOT NULL"
        )
        return [row["ref"] for row in rows]

    async def photo_recovery_metadata(self, ref: str):
        rows = await self._run(
            "SELECT i.id AS identity_id, i.crop_ref, i.crop_ref_side, i.awb_id, "
            "a.id, a.awb_number, a.batch_id, a.status, a.parcel_id, a.created_at, "
            "a.hawb_number, a.part, a.total, a.label_photo_ref, a.timecreated "
            "FROM cv.parcel_identity i LEFT JOIN cv.awb a ON a.id=i.awb_id "
            "WHERE i.crop_ref=$1 OR i.crop_ref_side=$1", ref)
        if len(rows) != 1:
            raise ValueError("photo has ambiguous parcel ownership")
        row = rows[0]
        if (row["crop_ref"] == ref) == (row["crop_ref_side"] == ref):
            raise ValueError("photo has ambiguous view")
        if row["awb_id"] is not None and (row["id"] is None or row["status"] != "PAIRED" or row["parcel_id"] != row["identity_id"]):
            raise ValueError("photo has inconsistent AWB ownership")
        return ("top" if row["crop_ref"] == ref else "side", _map_awb(row) if row["awb_id"] is not None else None)

    async def photo_references_existing(self, refs: list[str]) -> set[str]:
        if not refs:
            return set()
        rows = await self._run(
            "SELECT label_photo_ref AS ref FROM cv.awb WHERE label_photo_ref = ANY($1::text[]) "
            "UNION SELECT crop_ref FROM cv.parcel_identity WHERE crop_ref = ANY($1::text[]) "
            "UNION SELECT crop_ref_side FROM cv.parcel_identity WHERE crop_ref_side = ANY($1::text[])", refs,
        )
        return {row["ref"] for row in rows}

    async def label_mawbs(self, hawb_number: str) -> list[str]:
        return [row["mawb"] for row in await self._run(
            "SELECT DISTINCT mawb FROM cv.label_print WHERE hawb=$1 ORDER BY mawb", hawb_number
        )]

    async def import_labels(self, rows):
        assert self._pool is not None
        inserted = updated = 0
        async with self._pool.acquire() as conn, conn.transaction():
            conn = _WarehouseConnection(conn)
            for row in rows:
                created = await conn.fetchval("""INSERT INTO cv.label_print
                    (mawb, pcs_mawb, hawb, pcs_hawb, part, airlines_name, flight_no, route)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (mawb, hawb, part) DO UPDATE SET
                    pcs_mawb=excluded.pcs_mawb, pcs_hawb=excluded.pcs_hawb,
                    airlines_name=excluded.airlines_name, flight_no=excluded.flight_no,
                    route=excluded.route, updated_at=now()
                    RETURNING (xmax = 0)""", *row)
                if created:
                    inserted += 1
                else:
                    updated += 1
        return {"imported": inserted, "updated": updated, "skipped": updated}

    async def delete_labels(self, ids):
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            conn = _WarehouseConnection(conn)
            result = await conn.execute("DELETE FROM cv.label_print WHERE id = ANY($1::bigint[])", ids)
        return int(result.split()[-1])

    # -- mapping -----------------------------------------------------------
    @staticmethod
    def _map(row) -> IdentityRecord:
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
            hu_moments=hu_from_json(row["hu_moments"]),
            crop_ref=row["crop_ref"],
            crop_ref_side=row["crop_ref_side"],
            awb_id=row["awb_id"],
            awb_number=row["awb_number"],
            created_at=row["created_at"],
        )

    _SELECT = """
        SELECT i.*, a.awb_number
        FROM cv.parcel_identity i
        LEFT JOIN cv.awb a ON a.id = i.awb_id
    """

    # -- parcels -----------------------------------------------------------
    async def get_by_track(self, batch_id: str, track_id: int) -> IdentityRecord | None:
        row = await self._runrow(f"{self._SELECT} WHERE i.batch_id=$1 AND i.track_id=$2", batch_id, track_id)
        return self._map(row) if row else None

    async def get(self, identity_id: int) -> IdentityRecord | None:
        row = await self._runrow(f"{self._SELECT} WHERE i.id=$1", identity_id)
        return self._map(row) if row else None

    async def get_many_metadata(self, identity_ids: list[int]) -> dict[int, IdentityRecord]:
        if not identity_ids:
            return {}
        rows = await self._run(
            "SELECT id, batch_id, track_id, status, idx, sim_score, crop_ref, crop_ref_side, created_at "
            "FROM cv.parcel_identity WHERE id = ANY($1::bigint[])", identity_ids,
        )
        return {row["id"]: IdentityRecord(**dict(row)) for row in rows}

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
        cfg = self._settings.dedup
        batch_filter, since = scope_window(cfg, batch_id)
        conditions = ["i.model_name=$2", "i.model_version=$3", "i.embedding IS NOT NULL"]
        params: list = [embedding, model_name, model_version]
        if batch_filter is not None:
            params.append(batch_filter)
            conditions.append(f"i.batch_id=${len(params)}")
        if since is not None:
            params.append(since)
            conditions.append(f"i.created_at >= ${len(params)}")
        if exclude_id is not None:
            params.append(exclude_id)
            conditions.append(f"i.id <> ${len(params)}")
        where = " AND ".join(conditions)
        params.append(cfg.top_k)
        # canonical embeddings (HNSW on parcel_identity.embedding)
        rows = await self._run(
            f"""
            SELECT i.*, a.awb_number, 1 - (i.embedding <=> $1) AS sim, 0 AS orientation
            FROM cv.parcel_identity i
            LEFT JOIN cv.awb a ON a.id = i.awb_id
            WHERE {where}
            ORDER BY i.embedding <=> $1
            LIMIT ${len(params)}
            """,
            *params,
        )
        matches: dict[int, SimilarMatch] = {}
        for row in rows:
            rec = self._map({k: row[k] for k in row.keys() if k != "sim" and k != "orientation"})
            matches[rec.id] = SimilarMatch(rec, float(row["sim"]), int(row["orientation"]))
        # additional orientation variants, if any
        rows = await self._run(
            f"""
            SELECT i.*, a.awb_number, 1 - (e.embedding <=> $1) AS sim, e.orientation
            FROM cv.parcel_embedding e
            JOIN cv.parcel_identity i ON i.id = e.identity_id
            LEFT JOIN cv.awb a ON a.id = i.awb_id
            WHERE {where}
            ORDER BY e.embedding <=> $1
            LIMIT ${len(params)}
            """,
            *params,
        )
        for row in rows:
            sim, orient = float(row["sim"]), int(row["orientation"])
            rec = self._map({k: row[k] for k in row.keys() if k != "sim" and k != "orientation"})
            if rec.id not in matches or sim > matches[rec.id].similarity:
                matches[rec.id] = SimilarMatch(rec, sim, orient)
        kept = filter_candidates_by_shape(list(matches.values()), shape, cfg)
        kept.sort(key=lambda m: m.similarity, reverse=True)
        return kept[: cfg.top_k]

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
        row = await self._runrow(
            """
            INSERT INTO cv.parcel_identity
                (batch_id, track_id, status, duplicate_of, sim_score, review_reason,
                 model_name, model_version, area, aspect_ratio, hu_moments, crop_ref, embedding)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            RETURNING id
            """,
            batch_id,
            track_id,
            status,
            duplicate_of,
            sim_score,
            review_reason,
            model_name,
            model_version,
            shape.area,
            shape.aspect_ratio,
            json_dumps_hu(shape.hu),
            crop_ref,
            embedding,
        )
        assert row is not None
        rec = await self.get(row["id"])
        assert rec is not None
        return rec

    async def _insert_variant(
        self,
        identity_id: int,
        embedding: np.ndarray | None,
        model_name: str,
        model_version: str,
        orientation: int,
    ) -> None:
        await self._run(
            """
            INSERT INTO cv.parcel_embedding (identity_id, model_name, model_version, orientation, embedding)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (identity_id, orientation) DO NOTHING
            """,
            identity_id,
            model_name,
            model_version,
            orientation,
            embedding,
        )

    async def finalize_new(
        self,
        *,
        batch_id: str,
        track_id: int,
        embedding: np.ndarray,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim_score: float | None,
        require_awb: bool,
        crop_ref_side: str | None = None,
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            conn = _WarehouseConnection(conn)
            async with conn.transaction():
                # per-batch advisory lock: serialized idx + AWB pairing
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", batch_id)
                existing = await conn.fetchrow(
                    "SELECT id FROM cv.parcel_identity WHERE batch_id=$1 AND track_id=$2",
                    batch_id,
                    track_id,
                )
                if existing:
                    rec = await self._get_in_conn(conn, existing["id"])
                    return rec, None
                idx = await conn.fetchval(
                    "SELECT COALESCE(MAX(idx), -1) + 1 FROM cv.parcel_identity WHERE batch_id=$1 AND idx IS NOT NULL",
                    batch_id,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO cv.parcel_identity
                        (batch_id, track_id, idx, status, sim_score, model_name, model_version,
                         area, aspect_ratio, hu_moments, crop_ref, embedding, crop_ref_side)
                    VALUES ($1,$2,$3,'NEW',$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    RETURNING id
                    """,
                    batch_id,
                    track_id,
                    idx,
                    sim_score,
                    model_name,
                    model_version,
                    shape.area,
                    shape.aspect_ratio,
                    json_dumps_hu(shape.hu),
                    crop_ref,
                    embedding,
                    crop_ref_side,
                )
                awb = await self._pair_next_awb_conn(conn, row["id"], batch_id)
                if require_awb and awb is None:
                    raise AwbRequiredError("no WAITING AWB available for NEW parcel")
                rec = await self._get_in_conn(conn, row["id"])
                return rec, awb

    async def _get_in_conn(self, conn: asyncpg.Connection, identity_id: int) -> IdentityRecord:
        row = await conn.fetchrow(f"{self._SELECT} WHERE i.id=$1", identity_id)
        assert row is not None
        return self._map(row)

    @staticmethod
    async def _pair_next_awb_conn(
        conn: asyncpg.Connection, parcel_id: int, batch_id: str
    ) -> AwbRecord | None:
        row = await conn.fetchrow(
            """
            UPDATE cv.awb SET status='PAIRED', parcel_id=$1, paired_at=now()
            WHERE id = (
                SELECT id FROM cv.awb
                WHERE status='WAITING' AND (batch_id IS NULL OR batch_id=$2)
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, awb_number, batch_id, status, parcel_id, created_at, hawb_number, part, total, label_photo_ref, timecreated
            """,
            parcel_id,
            batch_id,
        )
        if row is not None:
            await conn.execute("UPDATE cv.parcel_identity SET awb_id=$2 WHERE id=$1", parcel_id, row["id"])
        return _map_awb(row) if row else None

    async def promote_to_new(
        self, identity_id: int, *, require_awb: bool
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            conn = _WarehouseConnection(conn)
            async with conn.transaction():
                rec_row = await conn.fetchrow(
                    "SELECT batch_id FROM cv.parcel_identity WHERE id=$1", identity_id
                )
                if rec_row is None:
                    raise KeyError(f"identity {identity_id} not found")
                batch_id = rec_row["batch_id"]
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", batch_id)
                idx = await conn.fetchval(
                    "SELECT COALESCE(MAX(idx), -1) + 1 FROM cv.parcel_identity WHERE batch_id=$1 AND idx IS NOT NULL",
                    batch_id,
                )
                await conn.execute(
                    "UPDATE cv.parcel_identity SET status='NEW', idx=$2, review_reason=NULL WHERE id=$1",
                    identity_id,
                    idx,
                )
                awb = await self._pair_next_awb_conn(conn, identity_id, batch_id)
                if require_awb and awb is None:
                    raise AwbRequiredError("no WAITING AWB available for promoted parcel")
                await conn.execute(
                    "INSERT INTO cv.review_audit (identity_id, action, operator, reason, model_version)"
                    " VALUES ($1,'NOT_DUPLICATE',$2,$3,NULL)",
                    identity_id,
                    "operator",
                    "resolved to NEW",
                )
                rec = await self._get_in_conn(conn, identity_id)
                return rec, awb

    async def resolve(
        self, identity_id: int, action: str, operator: str, reason: str | None
    ) -> IdentityRecord | None:
        if action not in ("CONFIRM_DUPLICATE", "RESCAN_INTENTIONAL"):
            raise ValueError(
                f"resolve action must be CONFIRM_DUPLICATE or RESCAN_INTENTIONAL, got {action!r}"
            )
        target = await self.confirm_duplicate_target(identity_id)
        new_status = "DUPLICATE"
        row = await self._runrow(
            """
            UPDATE cv.parcel_identity
            SET status=$2, duplicate_of=COALESCE(duplicate_of, $3)
            WHERE id=$1 RETURNING id
            """,
            identity_id,
            new_status,
            target.id if target else None,
        )
        if row is None:
            return None
        await self.insert_audit(identity_id, action, operator, reason, None, None)
        return await self.get(identity_id)

    async def confirm_duplicate_target(self, identity_id: int) -> IdentityRecord | None:
        rec = await self.get(identity_id)
        if rec is None:
            return None
        if rec.duplicate_of is not None:
            return await self.get(rec.duplicate_of)
        # no stored match (e.g. embedding failed) -> None
        return None

    async def pending_reviews(self) -> list[IdentityRecord]:
        rows = await self._run(f"{self._SELECT} WHERE i.status='REVIEW' ORDER BY i.created_at")
        return [self._map(r) for r in rows]

    async def add_side_capture(
        self,
        identity_id: int,
        embedding: np.ndarray | None,
        crop_ref: str,
        *,
        model_name: str,
        model_version: str,
    ) -> None:
        await self._run("UPDATE cv.parcel_identity SET crop_ref_side=$2 WHERE id=$1", identity_id, crop_ref)
        if embedding is not None:
            await self._insert_variant(identity_id, embedding, model_name, model_version, 1)

    async def list_audit(self, identity_id: int) -> list[dict]:
        rows = await self._run(
            "SELECT to_char(at, 'YYYY-MM-DD HH24:MI:SS') AS at, action, operator, reason, sim_score"
            " FROM cv.review_audit WHERE identity_id=$1 ORDER BY at DESC",
            identity_id,
        )
        return [dict(r) for r in rows]

    async def insert_audit(
        self,
        identity_id: int,
        action: str,
        operator: str,
        reason: str | None,
        sim_score: float | None,
        model_version: str | None,
    ) -> None:
        await self._run(
            """
            INSERT INTO cv.review_audit (identity_id, action, operator, reason, sim_score, model_version)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            identity_id,
            action,
            operator,
            reason,
            sim_score,
            model_version,
        )

    async def last_idx(self, batch_id: str) -> int:
        val = await self._runrow(
            "SELECT COALESCE(MAX(idx), -1) AS m FROM cv.parcel_identity WHERE batch_id=$1 AND idx IS NOT NULL",
            batch_id,
        )
        return int(val["m"]) if val else -1

    # -- AWBs ---------------------------------------------------------------
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
        row = await self._runrow(
            """
            INSERT INTO cv.awb (awb_number, batch_id, hawb_number, part, total, timecreated)
            VALUES ($1,$2,$3,$4,$5,CASE WHEN $6::boolean THEN now() ELSE NULL END)
            RETURNING id, awb_number, batch_id, status, parcel_id, created_at, hawb_number, part, total, label_photo_ref, timecreated
            """,
            awb_number,
            batch_id,
            hawb_number,
            part,
            total,
            qr_scanned,
        )
        return _map_awb(row)

    async def list_awbs(self, status: str | None = None) -> list[AwbRecord]:
        if status:
            rows = await self._run(
                "SELECT id, awb_number, batch_id, status, parcel_id, created_at, hawb_number, part, total, label_photo_ref, timecreated FROM cv.awb WHERE status=$1 ORDER BY created_at",
                status,
            )
        else:
            rows = await self._run(
                "SELECT id, awb_number, batch_id, status, parcel_id, created_at, hawb_number, part, total, label_photo_ref, timecreated FROM cv.awb ORDER BY created_at"
            )
        return [_map_awb(r) for r in rows]

    async def attach_label_photo(self, awb_id: int, ref: str) -> bool:
        row = await self._runrow(
            "UPDATE cv.awb SET label_photo_ref=$2 WHERE id=$1 AND label_photo_ref IS NULL RETURNING id",
            awb_id, ref,
        )
        return row is not None

    async def delete_awb(self, awb_id: int, *, force: bool = False) -> bool:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            conn = _WarehouseConnection(conn)
            async with conn.transaction():
                awb = await conn.fetchrow("SELECT awb_number, status FROM cv.awb WHERE id=$1", awb_id)
                if awb is None:
                    return False
                if awb["status"] == "PAIRED":
                    if not force:
                        return False
                    unlinked = await conn.fetch(
                        "UPDATE cv.parcel_identity SET awb_id=NULL WHERE awb_id=$1 RETURNING id",
                        awb_id,
                    )
                    for row in unlinked:
                        await conn.execute(
                            "INSERT INTO cv.review_audit (identity_id, action, operator, reason)"
                            " VALUES ($1, 'AWB_FORCE_UNLINK', 'operator', $2)",
                            row["id"],
                            f"AWB {awb['awb_number']} force-deleted; parcel unlinked",
                        )
                await conn.execute("DELETE FROM cv.awb WHERE id=$1", awb_id)
                return True


def json_dumps_hu(hu: list[float]) -> str:
    import json

    return json.dumps(hu)


def _map_awb(row) -> AwbRecord:
    return AwbRecord(
        id=row["id"],
        awb_number=row["awb_number"],
        batch_id=row["batch_id"],
        status=row["status"],
        parcel_id=row["parcel_id"],
        created_at=row["created_at"],
        hawb_number=row["hawb_number"],
        part=row["part"],
        total=row["total"],
        label_photo_ref=row.get("label_photo_ref"),
        timecreated=row.get("timecreated"),
    )
