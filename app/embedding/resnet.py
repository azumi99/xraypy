"""ResNet18/50 embedders (torchvision, ImageNet weights, fc -> Identity)."""

from __future__ import annotations

import numpy as np
import torch
import torchvision
from torchvision.models import resnet18 as _resnet18
from torchvision.models import resnet50 as _resnet50

from .base import Embedder


class ResNetEmbedder(Embedder):
    def __init__(self, variant: str, device: str = "cpu", half: bool = False) -> None:
        if variant not in {"resnet18", "resnet50"}:
            raise ValueError(f"unsupported ResNet variant: {variant!r}")
        self.name = variant
        weights_tag = "IMAGENET1K_V1"
        if variant == "resnet18":
            weights = torchvision.models.ResNet18_Weights.IMAGENET1K_V1
            self.dim = 512
            builder = _resnet18
        else:
            weights = torchvision.models.ResNet50_Weights.IMAGENET1K_V1
            self.dim = 2048
            builder = _resnet50
        self._model = builder(weights=weights)
        self._model.fc = torch.nn.Identity()
        self.version = f"torchvision-{weights_tag}"
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
        features = self._model(batch)
        return Embedder.l2_normalize(features.float().cpu().numpy())
