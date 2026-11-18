"""DINOv2 embedders via Hugging Face transformers (facebook/dinov2-small|base).

Pooling (from config, see EmbeddingConfig.pooling):
- ``cls``:       CLS token only (dim 384 / 768).
- ``mean``:      mean over patch tokens (excludes CLS).
- ``cls_mean``:  concatenation of CLS and mean-patch (dim doubles), then
                 re-normalized after concatenation.
"""

from __future__ import annotations

import numpy as np
import torch

from .base import Embedder

HF_MODEL_IDS = {
    "dinov2_small": "facebook/dinov2-small",
    "dinov2_base": "facebook/dinov2-base",
}
BASE_DIMS = {"dinov2_small": 384, "dinov2_base": 768}


class DinoV2Embedder(Embedder):
    def __init__(
        self,
        variant: str,
        pooling: str = "cls",
        device: str = "cpu",
        half: bool = False,
    ) -> None:
        if variant not in HF_MODEL_IDS:
            raise ValueError(f"unsupported DINOv2 variant: {variant!r}")
        if pooling not in {"cls", "mean", "cls_mean"}:
            raise ValueError(f"unsupported pooling: {pooling!r}")
        self.name = variant
        self._pooling = pooling
        base_dim = BASE_DIMS[variant]
        self.dim = base_dim * 2 if pooling == "cls_mean" else base_dim
        self.version = f"hf-{HF_MODEL_IDS[variant]}"

        from transformers import AutoModel

        self._model = AutoModel.from_pretrained(HF_MODEL_IDS[variant])
        self._device = torch.device(device)
        self._half = half and self._device.type == "cuda"
        self._model.to(self._device)
        if self._half:
            self._model.half()
        self._model.eval()

    @torch.inference_mode()
    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = torch.from_numpy(np.stack(images)).to(self._device)
        if self._half:
            batch = batch.half()
        out = self._model(batch)
        hidden = out.last_hidden_state  # [N, 1 + P, D]
        cls = hidden[:, 0]
        if self._pooling == "cls":
            pooled = cls
        elif self._pooling == "mean":
            pooled = hidden[:, 1:].mean(dim=1)
        else:  # cls_mean
            pooled = torch.cat([cls, hidden[:, 1:].mean(dim=1)], dim=-1)
        return Embedder.l2_normalize(pooled.float().cpu().numpy())
