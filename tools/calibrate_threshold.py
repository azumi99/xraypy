"""Calibrate t_high / t_low from labelled pair scores.

Targets (defaults): False Duplicate rate <= 0.1%, duplicate Recall >= 98%.
Also reports the fraction of pairs falling into the REVIEW zone.

Usage:
  .venv/bin/python tools/calibrate_threshold.py pairs.json --target-far 0.001 --target-recall 0.98
pairs.json format: {"pos": [sim...], "neg": [sim...]}  (output of a benchmark
run or scores collected from your own scanner).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", type=Path)
    ap.add_argument("--target-far", type=float, default=0.001, help="max false-duplicate rate")
    ap.add_argument("--target-recall", type=float, default=0.98, help="min duplicate recall")
    args = ap.parse_args()

    data = json.loads(args.pairs.read_text())
    pos = np.asarray(data["pos"], dtype=np.float64)
    neg = np.asarray(data["neg"], dtype=np.float64)
    if len(pos) == 0 or len(neg) == 0:
        raise SystemExit("pairs.json must contain non-empty 'pos' and 'neg' arrays")

    # t_high: smallest threshold whose false-duplicate rate (neg >= t) <= target
    t_high = None
    for t in np.sort(neg)[::-1]:
        if float((neg >= t).mean()) <= args.target_far:
            t_high = float(t)
            break
    if t_high is None:
        t_high = float(neg.max())
        print(f"WARNING: cannot meet FAR<={args.target_far}; using max neg score {t_high:.4f}")

    # t_low: largest threshold whose duplicate recall (pos >= t_low..t_high) >= target
    t_low = None
    for t in np.sort(pos):
        recall = float((pos >= t).mean())
        if recall >= args.target_recall:
            t_low = float(t)
            break
    if t_low is None:
        t_low = float(pos.min())
        print(f"WARNING: cannot meet recall>={args.target_recall}; using min pos score {t_low:.4f}")

    zone = float(((pos >= t_low) & (pos < t_high)).mean() * 100)
    missed = float((pos < t_low).mean() * 100)
    false_dup = float((neg >= t_high).mean() * 100)

    print(
        f"""
Recommended config (dedup):
  t_high: {t_high:.4f}   # false duplicates: {false_dup:.3f}% (target <= {args.target_far * 100:.2f}%)
  t_low:  {t_low:.4f}   # missed duplicates (below zone): {missed:.3f}% (recall target >= {args.target_recall * 100:.1f}%)
REVIEW zone: {zone:.2f}% of positive pairs land in [t_low, t_high)

CATATAN: angka hanya valid untuk data pasangan yang diberikan. Kalibrasi ulang
dengan crop asli dari scanner Anda (tools/bench_embedders.py) sebelum produksi.
"""
    )


if __name__ == "__main__":
    main()
