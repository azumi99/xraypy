"""Model registry: pick the embedder from config. One active model at a time."""

from __future__ import annotations

import logging

import numpy as np

from ..config import EmbeddingConfig
from .base import Embedder

logger = logging.getLogger(__name__)

# Canonical vector dimension per (model, pooling). Used by the migration
# runner to size the pgvector column, and to validate stored rows.
MODEL_DIMS: dict[tuple[str, str], int] = {
    ("resnet18", "cls"): 512,
    ("resnet50", "cls"): 2048,
    ("dinov2_small", "cls"): 384,
    ("dinov2_small", "mean"): 384,
    ("dinov2_small", "cls_mean"): 768,
    ("dinov2_base", "cls"): 768,
    ("dinov2_base", "mean"): 768,
    ("dinov2_base", "cls_mean"): 1536,
}

# ImageNet statistics (ResNet + DINOv2 both use these).
NORMALIZATION = {
    "mean": (0.485, 0.456, 0.406),
    "std": (0.229, 0.224, 0.225),
}


def model_dim(model: str, pooling: str) -> int:
    try:
        return MODEL_DIMS[(model, pooling)]
    except KeyError:
        raise ValueError(f"unknown model/pooling combination: {model}/{pooling}") from None


class EmbedderLoadError(RuntimeError):
    """Model failed to load — service must refuse to start."""


def create_embedder(cfg: EmbeddingConfig) -> Embedder:
    """Instantiate and warm up the configured embedder.

    Weights come from the local HF/torch cache. Pre-download with:
        .venv/bin/python tools/preload_models.py
    """
    if (cfg.model, cfg.pooling) not in MODEL_DIMS:
        raise EmbedderLoadError(f"invalid embedding config: model={cfg.model} pooling={cfg.pooling}")
    try:
        if cfg.model.startswith("dinov2"):
            from .dinov2 import DinoV2Embedder

            embedder: Embedder = DinoV2Embedder(
                cfg.model, pooling=cfg.pooling, device=cfg.device, half=cfg.half
            )
        else:
            from .resnet import ResNetEmbedder

            embedder = ResNetEmbedder(cfg.model, device=cfg.device, half=cfg.half)
    except Exception as exc:
        raise EmbedderLoadError(
            f"failed to load embedding model {cfg.model} on {cfg.device}: {exc}. "
            "Pre-download weights with `.venv/bin/python tools/preload_models.py` "
            "and check HF_HOME/TORCH_HOME."
        ) from exc
    if embedder.dim != model_dim(cfg.model, cfg.pooling):
        raise EmbedderLoadError(
            f"model dim mismatch: got {embedder.dim}, registry says " f"{model_dim(cfg.model, cfg.pooling)}"
        )
    warmup(embedder, cfg)
    logger.info(
        "embedding model ready: %s (%s) dim=%d on %s",
        embedder.name,
        embedder.version,
        embedder.dim,
        cfg.device,
    )
    return embedder


def warmup(embedder: Embedder, cfg: EmbeddingConfig) -> None:
    """One dummy inference so lazy allocations happen before real traffic."""
    dummy = np.zeros((3, cfg.img_size, cfg.img_size), dtype=np.float32)  # CHW
    try:
        out = embedder.embed([dummy])
    except Exception as exc:
        raise EmbedderLoadError(f"warm-up inference failed for {embedder.name}: {exc}") from exc
    if out.shape != (1, embedder.dim):
        raise EmbedderLoadError(f"warm-up produced wrong shape {out.shape}, expected (1, {embedder.dim})")
