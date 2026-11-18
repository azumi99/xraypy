"""Pre-download model weights into the local cache (run before deploying).

Usage: .venv/bin/python tools/preload_models.py [model ...]
Models: dinov2_small dinov2_base resnet18 resnet50 yolo11n-seg
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import EmbeddingConfig, load_settings  # noqa: E402
from app.embedding.registry import create_embedder  # noqa: E402


def main() -> None:
    wanted = sys.argv[1:] or [load_settings().embedding.model, "yolo11n-seg"]
    for name in wanted:
        if name.startswith("yolo"):
            from ultralytics import YOLO

            YOLO(f"{name}.pt")
            print(f"OK  {name} (ultralytics cache)")
        else:
            cfg = EmbeddingConfig(model=name, device="cpu")  # type: ignore[arg-type]
            emb = create_embedder(cfg)
            print(f"OK  {name} dim={emb.dim}")


if __name__ == "__main__":
    main()
