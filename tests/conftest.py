"""Shared test fixtures: stub embedder, sqlite store, dedup service."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DedupConfig, EmbeddingConfig, Settings  # noqa: E402
from app.dedup.service import DedupService  # noqa: E402
from app.dedup.shape_filter import ShapeFeatures  # noqa: E402
from app.dedup.sqlite_store import SqliteStore  # noqa: E402
from app.embedding.base import Embedder  # noqa: E402
from app.embedding.worker import EmbeddingWorker  # noqa: E402


@pytest.fixture(autouse=True)
def disable_live_manifest_tunnels(monkeypatch):
    # Tests must never start real tunnels from a developer's opted-in .env.
    monkeypatch.setenv("MANIFEST_BGD_TUNNEL_ENABLED", "false")
    monkeypatch.setenv("MANIFEST_CAS_TUNNEL_ENABLED", "false")
    monkeypatch.setenv("CV_DB_TUNNEL_ENABLED", "false")


class StubEmbedder(Embedder):
    """Deterministic embedder: 8x8 gray downsample -> 64-D, L2-normalized."""

    name = "stub"
    version = "stub-v1"
    dim = 64

    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = []
        for img in images:
            # images are CHW float32 normalized; collapse to HxW, drop the
            # per-image mean so only structure (not brightness) is compared
            g = img.mean(axis=0)
            small = cv2_resize(g)
            v = small.astype(np.float32).flatten()
            v -= v.mean()
            out.append(v / max(np.linalg.norm(v), 1e-9))
        return np.stack(out)


def cv2_resize(g: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)


def make_settings(tmp_path: Path, **dedup_kwargs) -> Settings:
    return Settings(
        database_url="unused",
        sqlite_path=str(tmp_path / "cv.sqlite"),
        crop_dir=tmp_path / "crops",
        dedup=DedupConfig(store="sqlite", **dedup_kwargs),
        embedding=EmbeddingConfig(model="resnet18"),
    )


def make_bgra(w: int = 120, h: int = 80, value: int = 90, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bgr = np.full((h, w, 3), value, dtype=np.uint8)
    bgr += rng.integers(-10, 10, bgr.shape, dtype=np.int16).astype(np.uint8)
    alpha = np.full((h, w), 255, dtype=np.uint8)
    return np.dstack([bgr, alpha])


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def store(settings):
    s = SqliteStore(settings, path=settings.sqlite_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def worker():
    w = EmbeddingWorker(StubEmbedder(), EmbeddingConfig())
    w.start()
    yield w
    w.stop()


@pytest.fixture
async def dedup(settings, worker, store):
    return DedupService(settings, worker, store)


@pytest.fixture
def shape():
    return ShapeFeatures(area=5000.0, aspect_ratio=1.4, hu=[0.1] * 7)
