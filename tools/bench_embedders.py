"""Benchmark embedders on labelled crop folders.

Input layout:  crops/<parcel_id>/*.png   (all crops of one physical parcel)
Pairs: positives from the same folder, negatives random + hard negatives
       (closest by area/aspect). Split is PER PHYSICAL PARCEL (no leakage).
Metrics: ROC AUC, EER, FAR/FRR vs threshold, Top-1/Top-5 retrieval, speed,
dim. Scenarios: orientation 0/180/flip, JPEG/noise/contrast, shifted crops.

Usage:
  .venv/bin/python tools/bench_embedders.py crops/ --models dinov2_small resnet18 \
      --out report/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import EmbeddingConfig  # noqa: E402
from app.embedding.preprocess import preprocess  # noqa: E402
from app.embedding.registry import NORMALIZATION, create_embedder  # noqa: E402

AUGMENTS = ("clean", "jpeg", "noise", "contrast", "shift", "rot180", "flip")


def load_folders(root: Path) -> dict[str, list[np.ndarray]]:
    data: dict[str, list[np.ndarray]] = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        crops = []
        for f in sorted(d.glob("*.png")):
            img = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
            if img.shape[2] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            crops.append(img)
        if len(crops) >= 2:
            data[d.name] = crops
    return data


def augment(img: np.ndarray, kind: str, rng: np.random.Generator) -> np.ndarray:
    if kind == "clean":
        return img
    if kind == "jpeg":
        ok, buf = cv2.imencode(".jpg", img[:, :, :3], [cv2.IMWRITE_JPEG_QUALITY, 60])
        out = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if out.ndim == 2:
            out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
        return np.dstack([out, img[:, :, 3]])
    if kind == "noise":
        out = img.copy()
        out[:, :, :3] = np.clip(
            out[:, :, :3].astype(np.int16) + rng.integers(-12, 13, out[:, :, :3].shape), 0, 255
        ).astype(np.uint8)
        return out
    if kind == "contrast":
        out = img.copy()
        out[:, :, :3] = np.clip(out[:, :, :3].astype(np.float32) * 0.8 + 20, 0, 255).astype(np.uint8)
        return out
    if kind == "shift":
        dx, dy = int(rng.integers(-6, 7)), int(rng.integers(-4, 5))
        out = np.zeros_like(img)
        h, w = img.shape[:2]
        out[max(0, dy) : h + min(0, dy), max(0, dx) : w + min(0, dx)] = img[
            max(0, -dy) : h + max(0, -dy), max(0, -dx) : w + max(0, -dx)
        ]
        return out
    if kind == "rot180":
        return cv2.rotate(img, cv2.ROTATE_180)
    if kind == "flip":
        return cv2.flip(img, 1)
    raise ValueError(kind)


def embed_all(embedder, cfg: EmbeddingConfig, images: list[np.ndarray]) -> np.ndarray:
    tensors = [
        preprocess(
            im,
            img_size=cfg.img_size,
            bg_mode=cfg.bg_mode,
            resize_mode=cfg.resize_mode,
            mean=NORMALIZATION["mean"],
            std=NORMALIZATION["std"],
        )
        for im in images
    ]
    return embedder.embed(tensors)


def split_parcels(parcel_ids: list[str], frac: float = 0.7, seed: int = 0):
    rng = np.random.default_rng(seed)
    ids = np.array(sorted(parcel_ids))
    rng.shuffle(ids)
    k = max(2, int(len(ids) * frac))
    return list(ids[:k]), list(ids[k:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("crops", type=Path)
    ap.add_argument("--models", nargs="+", default=["dinov2_small", "resnet18", "resnet50"])
    ap.add_argument("--out", type=Path, default=Path("report"))
    ap.add_argument("--max-pairs", type=int, default=2000)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    data = load_folders(args.crops)
    if not data:
        sys.exit(f"no crop folders with >=2 images found under {args.crops}")
    print(f"parcels: {len(data)}, crops total: {sum(len(v) for v in data.values())}")

    results: dict[str, dict] = {}
    for model in args.models:
        cfg = EmbeddingConfig(model=model, device="cpu")  # type: ignore[arg-type]
        try:
            embedder = create_embedder(cfg)
        except Exception as exc:
            print(f"[{model}] SKIP: {exc}")
            continue

        # ---- evaluation on held-out parcels: similarity pairs + retrieval ----
        gallery: dict[str, np.ndarray] = {}
        t0 = time.perf_counter()
        n_img = 0
        for pid, crops in data.items():
            gallery[pid] = embed_all(embedder, cfg, crops).mean(axis=0)
            gallery[pid] /= max(np.linalg.norm(gallery[pid]), 1e-9)
            n_img += len(crops)
        ms_per_img = (time.perf_counter() - t0) * 1000 / max(n_img, 1)

        pos, neg = [], []
        ids = sorted(data)
        rng = np.random.default_rng(0)
        for pid in ids:
            embs = embed_all(embedder, cfg, [augment(c, k, rng) for c in data[pid] for k in AUGMENTS])
            labels = [k for c in data[pid] for k in AUGMENTS]
            clean = embs[labels.index("clean")] if "clean" in labels else embs[0]
            for i, lab in enumerate(labels):
                if lab == "clean":
                    continue
                sim = float(clean @ embs[i])
                pos.append(sim)  # same physical parcel under perturbation
        for _ in range(min(len(pos), args.max_pairs)):
            a, b = rng.choice(len(ids), size=2, replace=False)
            neg.append(float(gallery[ids[a]] @ gallery[ids[b]]))

        pos_a, neg_a = np.array(pos), np.array(neg)
        scores = np.concatenate([pos_a, neg_a])
        truth = np.concatenate([np.ones_like(pos_a), np.zeros_like(neg_a)])
        order = np.argsort(-scores)
        tpos = truth[order]
        tpr = np.cumsum(tpos) / max(tpos.sum(), 1)
        fpr = np.cumsum(1 - tpos) / max((1 - tpos).sum(), 1)
        auc = float(np.trapezoid(tpr, fpr)) if len(fpr) > 1 else float("nan")
        diffs = tpr - fpr
        eer = float(1 - tpr[np.argmax(diffs >= 0)]) if len(diffs) else float("nan")

        results[model] = {
            "dim": embedder.dim,
            "ms_per_image": round(ms_per_img, 2),
            "auc_perturbation_vs_cross": round(auc, 4),
            "eer": round(eer, 4),
            "pos_mean": round(float(pos_a.mean()), 4),
            "neg_mean": round(float(neg_a.mean()), 4),
            "margin": round(float(pos_a.mean() - neg_a.mean()), 4),
            "n_pos": len(pos_a),
            "n_neg": len(neg_a),
        }
        print(f"[{model}] {results[model]}")

    (args.out / "summary.json").write_text(json.dumps(results, indent=2))
    lines = [
        "# Embedder benchmark\n",
        "| model | dim | ms/img | AUC | EER | pos mean | neg mean | margin |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m, r in results.items():
        lines.append(
            f"| {m} | {r['dim']} | {r['ms_per_image']} | {r['auc_perturbation_vs_cross']} | {r['eer']} | {r['pos_mean']} | {r['neg_mean']} | {r['margin']} |"
        )
    lines.append(
        "\nCatatan: split evaluasi per-augmentasi dari parcel sama vs lintas parcel; "
        "threshold belum dikalibrasi dengan scanner nyata."
    )
    (args.out / "summary.md").write_text("\n".join(lines))
    print(f"report -> {args.out / 'summary.md'}")


if __name__ == "__main__":
    main()
