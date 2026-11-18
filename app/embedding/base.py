"""Embedder interface.

All implementations return an [N, D] float32 array, L2-normalized per row,
computed under ``torch.inference_mode()`` on an ``eval()`` model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Embedder(ABC):
    """Computes visual embeddings from preprocessed RGB uint8 images (HWC)."""

    name: str
    version: str
    dim: int

    @abstractmethod
    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        """Embed a batch of uint8 HWC RGB images -> float32 [N, D], L2-normalized."""
        raise NotImplementedError

    def embed_one(self, image: np.ndarray) -> np.ndarray:
        """Convenience: single image -> 1-D vector."""
        return self.embed([image])[0]

    @staticmethod
    def l2_normalize(vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        return (vectors / norms).astype(np.float32)
