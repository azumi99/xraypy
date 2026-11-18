"""Generate synthetic top-view conveyor demo video (X-ray screen style).

Simulasi layar mesin X-ray tampak atas: strip bergulir ke kanan, objek baru
masuk dari kiri — cocok untuk demo pipeline auto-capture cv-service.

Usage:
    .venv/bin/python tools/make_demo_video.py [output.mp4] [duration_sec]
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import cv2
import numpy as np

W, H = 1280, 720
FPS = 30
SPEED = 140.0  # px per detik, layar bergulir ke kanan
SPAWN_INTERVAL = 2.2  # detik antar objek

rng = random.Random(42)


def make_texture(w: int, h: int, kind: str) -> np.ndarray:
    """Tekstur internal objek ala densitas X-ray (grayscale + sedikit warna)."""
    base = rng.randint(70, 130)
    tex = np.full((h, w), base, np.float32)

    if kind == "box":  # kardus: noise halus + tepi gelap
        tex += rng.randint(-15, 25)
        tex += np.random.default_rng(rng.randint(0, 9999)).normal(0, 12, (h, w)).astype(np.float32)
        cv2.rectangle(tex, (0, 0), (w - 1, h - 1), 30, 3)
        if rng.random() < 0.7:  # lakban
            cv2.rectangle(tex, (w // 2 - 6, 0), (w // 2 + 6, h - 1), 160, -1)
    elif kind == "bottle":  # botol: badan terang, leher gelap
        tex[:] = 40
        body_top = int(h * 0.30)
        cv2.rectangle(tex, (int(w * 0.22), body_top), (int(w * 0.78), h - 4), 185, -1)
        cv2.rectangle(tex, (int(w * 0.40), 4), (int(w * 0.60), body_top), 70, -1)
        cv2.circle(tex, (w // 2, int(h * 0.62)), int(w * 0.16), 130, -1)
    elif kind == "envelope":  # amplop tipis: hampir transparan
        tex[:] = rng.randint(45, 65)
        for y in range(0, h, max(4, h // 12)):
            cv2.line(tex, (0, y), (w - 1, y), 90, 1)
        cv2.line(tex, (0, 0), (w - 1, h - 1), 110, 2)
        cv2.line(tex, (0, h - 1), (w - 1, 0), 110, 2)
    elif kind == "electronics":  # elektronik: blok + komponen
        tex[:] = 55
        for _ in range(rng.randint(6, 12)):
            x1, y1 = rng.randint(4, w - 40), rng.randint(4, h - 40)
            bw, bh = rng.randint(15, 45), rng.randint(10, 30)
            cv2.rectangle(tex, (x1, y1), (x1 + bw, y1 + bh), rng.randint(90, 210), -1)
        cv2.rectangle(tex, (2, 2), (w - 3, h - 3), 25, 2)
    else:  # softbag: blob halus
        tex += np.random.default_rng(rng.randint(0, 9999)).normal(0, 20, (h, w)).astype(np.float32)
        cv2.circle(tex, (w // 2, h // 2), min(w, h) // 3, rng.randint(120, 190), -1)

    return np.clip(tex, 0, 255).astype(np.uint8)


class Parcel:
    def __init__(self, x: float):
        kind = rng.choice(["box", "box", "bottle", "envelope", "electronics", "softbag"])
        if kind == "bottle":
            w, h = rng.randint(60, 90), rng.randint(150, 200)
        elif kind == "envelope":
            w, h = rng.randint(140, 200), rng.randint(50, 75)
        else:
            w, h = rng.randint(110, 220), rng.randint(90, 180)

        self.kind = kind
        self.w, self.h = w, h
        self.x = x  # posisi kiri
        self.y = rng.randint(80, H - 80 - h)
        self.angle = rng.uniform(-4, 4) if kind != "bottle" else 0.0
        self.sprite = self._render()

    def _render(self) -> np.ndarray:
        """Render sprite BGRA sekali di awal (rotasi termasuk)."""
        tex = make_texture(self.w, self.h, self.kind)
        bgr = cv2.cvtColor(tex, cv2.COLOR_GRAY2BGR)
        # tint warna tipis biar beda antar objek
        tint = np.array([rng.randint(-20, 30), rng.randint(-15, 25), rng.randint(-25, 20)])
        bgr = np.clip(bgr.astype(np.int16) + tint, 0, 255).astype(np.uint8)

        alpha = np.full((self.h, self.w), 255, np.uint8)
        # sudut membulat
        r = 10 if self.kind == "box" else 16
        mask = np.zeros((self.h, self.w), np.uint8)
        cv2.rectangle(mask, (r, 0), (self.w - r - 1, self.h - 1), 255, -1)
        cv2.rectangle(mask, (0, r), (self.w - 1, self.h - r - 1), 255, -1)
        for cx, cy in [(r, r), (self.w - r - 1, r), (r, self.h - r - 1), (self.w - r - 1, self.h - r - 1)]:
            cv2.circle(mask, (cx, cy), r, 255, -1)
        alpha = cv2.bitwise_and(alpha, mask)

        bgra = np.dstack([bgr, alpha])
        if abs(self.angle) > 0.1:
            M = cv2.getRotationMatrix2D((self.w / 2, self.h / 2), self.angle, 1.0)
            cos, sin = abs(M[0, 0]), abs(M[0, 1])
            nw, nh = int(self.h * sin + self.w * cos), int(self.h * cos + self.w * sin)
            M[0, 2] += nw / 2 - self.w / 2
            M[1, 2] += nh / 2 - self.h / 2
            bgra = cv2.warpAffine(bgra, M, (nw, nh), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
            self.w, self.h = nw, nh
        return bgra

    def done_entering(self) -> bool:
        return self.x >= 8

    def offscreen(self) -> bool:
        return self.x > W + 20


def background() -> np.ndarray:
    """Latar gelap khas layar X-ray + garis belt."""
    bg = np.zeros((H, W, 3), np.uint8)
    for y in range(H):
        v = int(14 + 10 * (y / H))
        bg[y, :] = (v, v + 2, v + 6)
    # garis tepi belt
    cv2.line(bg, (0, 60), (W, 60), (60, 70, 80), 2)
    cv2.line(bg, (0, H - 60), (W, H - 60), (60, 70, 80), 2)
    # roller belt
    for x in range(0, W, 46):
        cv2.line(bg, (x, 62), (x, H - 62), (35, 40, 46), 1)
    return bg


def overlay(frame: np.ndarray, t: float, n_parcel: int) -> None:
    scan_y = int((t * SPEED) % (H + 200)) - 100
    cv2.line(frame, (0, scan_y), (W, scan_y), (0, 140, 200), 1)
    cv2.putText(frame, "TOP-VIEW CONVEYOR  |  DEMO", (18, 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 220, 240), 1, cv2.LINE_AA)
    cv2.putText(frame, f"t={t:05.1f}s  parcels={n_parcel}", (W - 280, 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (160, 190, 220), 1, cv2.LINE_AA)


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "demo_objek_tampak_atas.mp4"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"avc1")
    writer = cv2.VideoWriter(str(out_path), fourcc, FPS, (W, H))
    if not writer.isOpened():  # fallback encoder
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
        if not writer.isOpened():
            raise SystemExit("Gagal membuka VideoWriter")
        out_path = out_path.with_suffix(".mp4v.mp4")
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))

    bg = background()
    parcels: list[Parcel] = [Parcel(-rng.randint(60, 300)) for _ in range(3)]
    total_frames = int(duration * FPS)
    n_captured = 0

    for i in range(total_frames):
        t = i / FPS
        # spawn objek baru dari kiri
        if t > 0 and i % int(SPAWN_INTERVAL * FPS) == 0:
            parcels.append(Parcel(float(-rng.randint(40, 160))))

        frame = bg.copy()
        # geser roller belt
        off = int(t * SPEED) % 46
        for x in range(-off, W, 46):
            cv2.line(frame, (x, 62), (x, H - 62), (38, 43, 50), 1)

        for p in parcels:
            p.x += SPEED / FPS
            x, y = int(p.x), int(p.y)
            sw, sh = p.w, p.h
            if x + sw > 0 and x < W and y + sh > 0 and y < H:
                x0, y0 = max(0, -x), max(0, -y)
                x1, y1 = min(sw, W - x), min(sh, H - y)
                roi = frame[y + y0:y + y1, x + x0:x + x1]
                spr = p.sprite[y0:y1, x0:x1]
                a = spr[:, :, 3:4].astype(np.float32) / 255.0
                roi[:] = (spr[:, :, :3].astype(np.float32) * a + roi.astype(np.float32) * (1 - a)).astype(np.uint8)
        parcels = [p for p in parcels if not p.offscreen()]

        for p in parcels:
            if p.done_entering() and not getattr(p, "_counted", False):
                p._counted = True
                n_captured += 1

        overlay(frame, t, n_captured)
        writer.write(frame)

    writer.release()
    print(f"OK: {out_path}  ({duration:.0f}s, {total_frames} frames, {n_captured} objek selesai masuk)")


if __name__ == "__main__":
    main()
