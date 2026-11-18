"""Re-compute embeddings for stored parcels from their saved crops.

Used when switching the embedding model: run this, then the config change is
the only other step — no code changes. Resumable: rows whose (model,
version) already match are skipped; safe to interrupt and re-run. While it
runs, searches only match rows of the ACTIVE model, so vectors are never
mixed across models.

Usage: .venv/bin/python tools/reembed.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.dedup.shape_filter import compute_shape_features  # noqa: E402
from app.embedding.preprocess import preprocess  # noqa: E402
from app.embedding.registry import NORMALIZATION, create_embedder  # noqa: E402
from app.embedding.worker import EmbeddingWorker  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max rows to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    embedder = create_embedder(settings.embedding)
    worker = EmbeddingWorker(embedder, settings.embedding)
    worker.start()
    store = __import__("app.dedup.store", fromlist=["make_store"]).make_store(settings)
    await store.initialize()

    # Re-open the pending set through plain SQL to avoid pgvector specifics.
    if settings.dedup.store == "pgvector":
        import asyncpg

        conn = await asyncpg.connect(settings.database_url)
        rows = await conn.fetch(
            "SELECT id, crop_ref FROM cv.parcel_identity_bgd WHERE crop_ref IS NOT NULL"
            " AND (model_name <> $1 OR model_version <> $2) ORDER BY id"
            + (" LIMIT $3" if args.limit else ""),
            embedder.name,
            embedder.version,
            *([args.limit] if args.limit else []),
        )
    else:
        import sqlite3

        conn = sqlite3.connect(settings.sqlite_path)
        q = "SELECT id, crop_ref FROM parcel_identity_bgd WHERE crop_ref IS NOT NULL AND (model_name<>? OR model_version<>?) ORDER BY id"
        rows = conn.execute(q, (embedder.name, embedder.version)).fetchall()
        if args.limit:
            rows = rows[: args.limit]

    print(f"{len(rows)} rows to re-embed with {embedder.name} ({embedder.version})")
    done = skipped = failed = 0
    for i, (identity_id, crop_ref) in enumerate(rows, 1):
        crop_path = Path(crop_ref)
        if not crop_path.is_absolute():
            crop_path = Path.cwd() / crop_path
        if not crop_path.exists():
            skipped += 1
            continue
        if args.dry_run:
            done += 1
            continue
        img = cv2_imread_bgra(crop_path)
        if img is None:
            failed += 1
            continue
        tensor = preprocess(
            img,
            img_size=settings.embedding.img_size,
            bg_mode=settings.embedding.bg_mode,
            resize_mode=settings.embedding.resize_mode,
            mean=NORMALIZATION["mean"],
            std=NORMALIZATION["std"],
        )
        emb = embedder.embed([tensor])[0]
        shape = compute_shape_features(img)
        if settings.dedup.store == "pgvector":
            from pgvector.asyncpg import register_vector

            await register_vector(conn)
            async with conn.transaction():
                await conn.execute(
                    "UPDATE cv.parcel_identity_bgd SET embedding=$2, model_name=$3, model_version=$4,"
                    " area=$5, aspect_ratio=$6, hu_moments=$7 WHERE id=$1",
                    identity_id,
                    emb,
                    embedder.name,
                    embedder.version,
                    shape.area,
                    shape.aspect_ratio,
                    json.dumps(shape.hu),
                )
        else:
            conn.execute(
                "UPDATE parcel_identity_bgd SET model_name=?, model_version=?, area=?, aspect_ratio=?, hu_moments=? WHERE id=?",
                (
                    embedder.name,
                    embedder.version,
                    shape.area,
                    shape.aspect_ratio,
                    json.dumps(shape.hu),
                    identity_id,
                ),
            )
            conn.execute("DELETE FROM parcel_embedding_bgd WHERE identity_id=?", (identity_id,))
            conn.execute(
                "INSERT INTO parcel_embedding_bgd (identity_id, model_name, model_version, orientation, embedding)"
                " VALUES (?,?,?,0,?)",
                (identity_id, embedder.name, embedder.version, emb.astype(np.float32).tobytes()),
            )
            conn.commit()
        done += 1
        if i % 25 == 0 or i == len(rows):
            print(f"  progress: {i}/{len(rows)} (ok={done} skip={skipped} fail={failed})")

    print(f"finished: ok={done} skipped={skipped} failed={failed}")
    worker.stop()
    await store.close()
    if settings.dedup.store == "pgvector":
        await conn.close()


def cv2_imread_bgra(path: Path) -> np.ndarray | None:
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img


if __name__ == "__main__":
    asyncio.run(main())
