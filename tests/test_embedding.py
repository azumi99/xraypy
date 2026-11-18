"""Embedder unit tests with a stub model (no weights, no GPU)."""

from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import StubEmbedder


def test_l2_normalized():
    emb = StubEmbedder()
    img = np.random.default_rng(0).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    v = emb.embed([img])[0]
    assert v.shape == (emb.dim,)
    assert np.isclose(np.linalg.norm(v), 1.0)


def test_identical_images_identical_embeddings():
    emb = StubEmbedder()
    img = np.random.default_rng(0).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    v1, v2 = emb.embed([img, img.copy()])
    assert np.isclose(float(v1 @ v2), 1.0)


def test_different_images_lower_similarity():
    emb = StubEmbedder()
    a = np.zeros((64, 64, 3), dtype=np.uint8)
    b = np.full((64, 64, 3), 255, dtype=np.uint8)
    va, vb = emb.embed([a, b])
    assert float(va @ vb) < 0.99


def test_empty_batch():
    emb = StubEmbedder()
    out = emb.embed([])
    assert out.shape == (0, emb.dim)


def test_queue_full_raises():
    from app.config import EmbeddingConfig
    from app.embedding.worker import EmbeddingWorker, QueueFullError

    w = EmbeddingWorker(StubEmbedder(), EmbeddingConfig(queue_max=1))
    # worker NOT started: nothing consumes the queue -> second submit overflows
    import asyncio

    loop = asyncio.new_event_loop()
    img = np.zeros((8, 8, 4), dtype=np.uint8)
    w.submit(loop, img)
    with pytest.raises(QueueFullError):
        w.submit(loop, img)
    loop.close()


@pytest.mark.slow
def test_real_dinov2_similarity():
    """Requires downloaded weights (tools/preload_models.py) — run manually."""
    from app.config import EmbeddingConfig
    from app.embedding.preprocess import preprocess
    from app.embedding.registry import NORMALIZATION, create_embedder

    emb = create_embedder(EmbeddingConfig(model="dinov2_small", device="cpu"))
    rng = np.random.default_rng(1)
    a = rng.integers(60, 200, (100, 100, 4), dtype=np.uint8)
    a[:, :, 3] = 255
    b = rng.integers(0, 255, (100, 100, 4), dtype=np.uint8)
    b[:, :, 3] = 255

    def pre(img):
        return preprocess(img, mean=NORMALIZATION["mean"], std=NORMALIZATION["std"])

    va, va2, vb = emb.embed([pre(a), pre(a.copy()), pre(b)])
    assert float(va @ va2) > 0.999
    assert float(va @ vb) < 0.99
