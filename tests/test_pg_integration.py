"""Integration tests against real Postgres+pgvector (local, no Docker needed).

Runs only when the target DB is reachable (default: the dev database
`object_cv`); otherwise skipped. Uses its own batch ids so it never clashes
with live data.

Run explicitly:  .venv/bin/python -m pytest tests/ -m slow
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from app.config import DedupConfig, EmbeddingConfig, Settings
from app.dedup.pg_store import PgVectorStore
from app.dedup.shape_filter import ShapeFeatures

pytestmark = pytest.mark.slow

DSN = "postgresql://ilhambintang@localhost:5432/object_cv"


def pg_settings() -> Settings:
    return Settings(
        database_url=DSN,
        dedup=DedupConfig(store="pgvector"),
        embedding=EmbeddingConfig(model="dinov2_small"),
    )


@pytest.fixture
def batch() -> str:
    return "test-" + uuid.uuid4().hex[:10]


@pytest.fixture
async def store():
    s = pg_settings()
    store = PgVectorStore(s)
    try:
        await store.initialize()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"pgvector not reachable: {exc}")
    yield store
    await store.close()


def _vec(seed: int, dim: int = 384) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim).astype(np.float32)
    return v / np.linalg.norm(v)


SHAPE = ShapeFeatures(area=5000.0, aspect_ratio=1.4, hu=[0.1] * 7)


async def test_finalize_new_assigns_sequential_idx(store, batch):
    r1, awb1 = await store.finalize_new(
        batch_id=batch,
        track_id=1,
        embedding=_vec(1),
        model_name="m",
        model_version="v",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    r2, _ = await store.finalize_new(
        batch_id=batch,
        track_id=2,
        embedding=_vec(2),
        model_name="m",
        model_version="v",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    assert (r1.idx, r2.idx) == (0, 1)


async def test_finalize_new_idempotent(store, batch):
    args = dict(
        batch_id=batch,
        track_id=7,
        embedding=_vec(3),
        model_name="m",
        model_version="v",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    r1, _ = await store.finalize_new(**args)
    r2, _ = await store.finalize_new(**args)
    assert r1.id == r2.id and r1.idx == r2.idx


async def test_find_similar_filters_model_and_batch(store, batch):
    await store.finalize_new(
        batch_id=batch,
        track_id=1,
        embedding=_vec(10),
        model_name="m",
        model_version="v1",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    v = _vec(10)
    same = await store.find_similar(v, model_name="m", model_version="v1", batch_id=batch, shape=None)
    assert same and same[0].similarity > 0.99
    other_model = await store.find_similar(v, model_name="m", model_version="v2", batch_id=batch, shape=None)
    assert other_model == []  # never mix vectors across models
    other_batch = await store.find_similar(
        v, model_name="m", model_version="v1", batch_id="other", shape=None
    )
    assert other_batch == []  # scope=batch


async def test_awb_fifo_pairing_transactional(store, batch):
    a1 = await store.insert_awb(f"T-{batch}-1")
    a2 = await store.insert_awb(f"T-{batch}-2")
    r1, awb1 = await store.finalize_new(
        batch_id=batch,
        track_id=1,
        embedding=_vec(21),
        model_name="m",
        model_version="v",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    r2, awb2 = await store.finalize_new(
        batch_id=batch,
        track_id=2,
        embedding=_vec(22),
        model_name="m",
        model_version="v",
        shape=SHAPE,
        crop_ref=None,
        sim_score=None,
        require_awb=False,
    )
    assert (awb1.awb_number, awb2.awb_number) == (a1.awb_number, a2.awb_number)
    rec = await store.get(r1.id)
    assert rec.awb_id == awb1.id  # backfilled on the parcel row too
