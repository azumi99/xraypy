"""Generate HiTraX-style top-view X-ray demo video.

Meniru tampilan `xray_8_top_1792824304409.jpg`: latar putih hangat, objek
organik oranye lembut (tas/pakaian) + objek padat biru tajam (botol,
elektronik), koper campuran, dan banner abu terang berlogo HiTraX.
Gerak belt pelan dengan jarak antar objek lebar dan berhenti sesaat.

Usage:
    .venv/bin/python tools/make_demo_hitrax.py [output.mp4] [duration_sec]
"""

from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

W, H = 640, 360
FPS = 30
SPEED = 32.0  # px per detik (pelan)
MIN_GAP_PX = 300  # jarak minimum antar objek
STOP_EVERY = (3.0, 5.0)  # detik bergerak sebelum berhenti (acak)
STOP_FOR = (1.0, 1.8)  # durasi berhenti sesaat (acak)
BANNER_H = 34

BG = (224, 226, 214)  # BGR putih hangat
BANNER_BG = (160, 166, 158)  # BGR abu terang
LOGO_BLUE = (180, 110, 30)  # BGR biru logo HiTraX
TEXT_DARK = (70, 70, 70)

ORANGE = (0, 70, 197)  # BGR oranye pekat (organik)
ORANGE_SOFT = (90, 150, 225)  # BGR oranye muda
BLUE = (243, 147, 101)  # BGR biru (logam/cairan padat)
BLUE_DEEP = (255, 120, 40)  # BGR biru tua (sangat padat)
GREEN = (120, 170, 90)  # BGR hijau (material campuran)

rng = random.Random(11)


def draw_bottle(bgr: np.ndarray, alpha: np.ndarray, cx: int, cy: int, rw: int, rh: int) -> None:
    """Botol biru: badan elips + leher + tutup, gaya X-ray."""
    cv2.ellipse(bgr, (cx, cy), (rw, rh), 0, 0, 360, BLUE, -1, cv2.LINE_AA)
    cv2.ellipse(bgr, (cx, cy - rh), (rw // 3, rh // 4), 0, 0, 360, BLUE, -1, cv2.LINE_AA)
    cv2.rectangle(bgr, (cx - rw // 4, cy - rh - rh // 4), (cx + rw // 4, cy - rh), BLUE_DEEP, -1)
    # kilau tengah (cairan lebih terang)
    cv2.ellipse(bgr, (cx, cy), (rw // 3, int(rh * 0.6)), 0, 0, 360, (250, 175, 130), -1, cv2.LINE_AA)
    # alpha: badan + leher + tutup
    cv2.ellipse(alpha, (cx, cy), (rw, rh), 0, 0, 360, 1.0, -1)
    cv2.ellipse(alpha, (cx, cy - rh), (rw // 3, rh // 4), 0, 0, 360, 1.0, -1)
    cv2.rectangle(alpha, (cx - rw // 4, cy - rh - rh // 4), (cx + rw // 4, cy - rh), 1.0, -1)


class Parcel:
    def __init__(self, x: float):
        self.kind = rng.choice(["bag", "clothes", "bottles", "electronics", "suitcase", "metal"])
        size = {
            "bag": (rng.randint(80, 130), rng.randint(60, 100)),
            "clothes": (rng.randint(70, 110), rng.randint(55, 85)),
            "bottles": (rng.randint(80, 120), rng.randint(70, 110)),
            "electronics": (rng.randint(90, 140), rng.randint(60, 90)),
            "suitcase": (rng.randint(130, 180), rng.randint(110, 150)),
            "metal": (rng.randint(60, 100), rng.randint(50, 90)),
        }[self.kind]
        self.w, self.h = size
        self.x = x
        self.y = rng.randint(8, H - BANNER_H - 8 - self.h)
        self.sprite = self._render()

    def _render(self) -> np.ndarray:
        s = 2
        w, h = self.w * s, self.h * s
        bgr = np.full((h, w, 3), BG, np.float32)
        alpha = np.zeros((h, w), np.float32)

        def blob(cx, cy, rw, rh, ang, color, dens):
            cv2.ellipse(bgr, (cx, cy), (rw, rh), ang, 0, 360, color, -1)
            cv2.ellipse(alpha, (cx, cy), (rw, rh), ang, 0, 360, dens, -1)

        def rect_area(x1, y1, x2, y2, color, dens):
            cv2.rectangle(bgr, (x1, y1), (x2, y2), color, -1)
            cv2.rectangle(alpha, (x1, y1), (x2, y2), dens, -1)

        if self.kind == "bag":
            for _ in range(6):
                blob(rng.randint(w // 4, w * 3 // 4), rng.randint(h // 3, h * 2 // 3),
                     rng.randint(w // 5, w // 3), rng.randint(h // 4, h // 3),
                     rng.uniform(-25, 25), ORANGE, rng.uniform(0.5, 0.95))
            blob(w // 2, h // 2, w // 3, h // 3, 0, ORANGE, 0.85)
            # tali/geurem
            cv2.line(bgr, (w // 3, h // 6), (w * 2 // 3, h * 5 // 6), ORANGE_SOFT, 3 * s // 2)
        elif self.kind == "clothes":
            for i in range(4):  # tumpukan lipatan
                y0 = int(h * (0.08 + 0.22 * i))
                rect_area(int(w * 0.06) + rng.randint(0, 8), y0,
                          int(w * 0.94) - rng.randint(0, 8), y0 + int(h * 0.2),
                          ORANGE if i % 2 == 0 else ORANGE_SOFT, rng.uniform(0.55, 0.9))
        elif self.kind == "bottles":
            # isi sekunder oranye di belakang botol
            blob(w // 2, h // 2, w // 2, h // 3, 0, ORANGE_SOFT, 0.4)
            for _ in range(rng.randint(3, 5)):
                draw_bottle(bgr, alpha, rng.randint(w // 5, w * 4 // 5), rng.randint(h // 3, h * 2 // 3),
                            rng.randint(8, 14) * s // 2, rng.randint(20, 32) * s // 2)
        elif self.kind == "electronics":
            rect_area(int(w * 0.1), int(h * 0.15), int(w * 0.9), int(h * 0.85), BLUE, 0.75)
            for _ in range(rng.randint(10, 16)):  # komponen papan sirkuit
                x1, y1 = rng.randint(int(w * 0.15), int(w * 0.7)), rng.randint(int(h * 0.2), int(h * 0.7))
                bw, bh = rng.randint(6, 20) * s // 2, rng.randint(4, 12) * s // 2
                rect_area(x1, y1, x1 + bw, y1 + bh,
                          (BLUE_DEEP, GREEN)[rng.random() < 0.25], 0.95)
            rect_area(int(w * 0.35), int(h * 0.62), int(w * 0.65), int(h * 0.85), BLUE_DEEP, 1.0)
        elif self.kind == "suitcase":
            # koper penuh: lapisan pakaian oranye + botol/elektronik biru
            rect_area(3, 3, w - 4, h - 4, ORANGE_SOFT, 0.35)  # dinding koper tipis
            for _ in range(10):  # pakaian memenuhi interior
                blob(rng.randint(w // 6, w * 5 // 6), rng.randint(h // 5, h * 4 // 5),
                     rng.randint(w // 7, w // 4), rng.randint(h // 7, h // 5),
                     rng.uniform(-30, 30), ORANGE, rng.uniform(0.45, 0.85))
            for _ in range(rng.randint(3, 5)):
                draw_bottle(bgr, alpha, rng.randint(w // 8, w * 7 // 8), rng.randint(h // 4, h * 3 // 4),
                            rng.randint(6, 11) * s // 2, rng.randint(16, 28) * s // 2)
            # blok elektronik biru
            bx, by = rng.randint(int(w * 0.15), int(w * 0.45)), rng.randint(int(h * 0.25), int(h * 0.55))
            rect_area(bx, by, bx + int(w * 0.28), by + int(h * 0.3), BLUE, 0.9)
            cv2.line(bgr, (bx + int(w * 0.14), 6), (bx + int(w * 0.14), h - 7), ORANGE_SOFT, 2)
        else:  # metal
            rect_area(int(w * 0.12), int(h * 0.12), int(w * 0.88), int(h * 0.88), BLUE_DEEP, 1.0)
            for _ in range(4):  # baut/screw terang
                cv2.circle(bgr, (rng.randint(w // 5, w * 4 // 5), rng.randint(h // 5, h * 4 // 5)),
                           rng.randint(3, 6) * s // 2, (170, 185, 210), -1)

        # mask bentuk keseluruhan: rounded rect
        shape = np.zeros((h, w), np.float32)
        r = 12 * s
        cv2.rectangle(shape, (r, 2), (w - r - 1, h - 3), 1.0, -1)
        cv2.rectangle(shape, (2, r), (w - 3, h - r - 1), 1.0, -1)
        for cx, cy in [(r, r), (w - r - 1, r), (r, h - r - 1), (w - r - 1, h - r - 1)]:
            cv2.circle(shape, (cx, cy), r, 1.0, -1)

        alpha = np.minimum(alpha, 1.0) * shape

        # tekstur mottled khas X-ray (noise halus) di dalam objek
        noise = np.random.default_rng(rng.randint(0, 99999)).normal(0, 14, (h, w, 1)).astype(np.float32)
        bgr = np.clip(bgr + noise, 0, 255)

        # blur lembut: organik menyatu, detail biru tetap terbaca
        blur_k = 9 if self.kind in ("bag", "clothes", "suitcase") else 5
        bgr = cv2.GaussianBlur(bgr, (blur_k, blur_k), 0)
        alpha = cv2.GaussianBlur(alpha, (5, 5), 0)
        alpha = np.clip(alpha, 0, 1)

        # tepi luar lebih pekat biar objek jelas
        edge = np.abs(cv2.Laplacian((alpha * 255).astype(np.uint8), cv2.CV_32F)) / 60.0
        alpha = np.clip(alpha + edge * 0.25, 0, 1)

        # alpha dasar supaya "transparan" khas X-ray tapi tetap jelas
        alpha = np.clip(0.5 + alpha * 0.5, 0, 1) * 255

        out = np.dstack([bgr, alpha]).astype(np.uint8)
        return cv2.resize(out, (self.w, self.h), interpolation=cv2.INTER_AREA)

    def offscreen(self) -> bool:
        return self.x > W + 20


def background() -> np.ndarray:
    bg = np.full((H, W, 3), BG, np.uint8)
    # pita lebih terang di atas, khas layar HiTraX
    cv2.rectangle(bg, (0, 0), (W, int(H * 0.08)), (232, 233, 222), -1)
    # banner bawah
    bg[H - BANNER_H:] = BANNER_BG
    cv2.line(bg, (0, H - BANNER_H), (W, H - BANNER_H), (140, 145, 138), 1)
    # pemisah kolom banner
    cv2.line(bg, (W - 130, H - BANNER_H), (W - 130, H - 14), (140, 145, 138), 1)
    return bg


def draw_banner(frame: np.ndarray, t: float) -> None:
    cv2.putText(frame, "HiTraX", (10, H - 10), cv2.FONT_HERSHEY_TRIPLEX, 0.7,
                LOGO_BLUE, 2, cv2.LINE_AA)
    stamp = datetime.now().strftime("%d.%m.%Y  %H:%M:%S")
    cv2.putText(frame, stamp, (W - 380, H - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                TEXT_DARK, 1, cv2.LINE_AA)
    cv2.putText(frame, "User: 0", (W - 380, H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                TEXT_DARK, 1, cv2.LINE_AA)
    # thumbnail strip kanan (miniatur dummy)
    x0 = W - 118
    cv2.rectangle(frame, (x0, H - BANNER_H + 4), (W - 4, H - 4), (210, 213, 205), -1)
    for i in range(3):
        cv2.rectangle(frame, (x0 + 8 + i * 34, H - BANNER_H + 8),
                      (x0 + 34 + i * 34, H - 8), ORANGE if i != 1 else BLUE, -1)


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path.home() / "Downloads" / "demo_orange_transparent_atas_perjelas.mp4"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    out_path.parent.mkdir(parents=True, exist_ok=True)

    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"avc1"), FPS, (W, H))
    if not writer.isOpened():
        writer.release()
        writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))

    bg = background()
    parcels: list[Parcel] = [Parcel(float(-rng.randint(20, 120)))]
    total_frames = int(duration * FPS)

    # state mesin gerak-berhenti (stop-and-go) belt
    phase_left = rng.uniform(*STOP_EVERY)
    moving = True
    px_since_spawn = float("inf")

    for i in range(total_frames):
        dt = 1.0 / FPS

        phase_left -= dt
        if phase_left <= 0.0:
            moving = not moving
            phase_left = rng.uniform(*(STOP_FOR if not moving else STOP_EVERY))

        dx = SPEED * dt if moving else 0.0
        px_since_spawn += dx

        if moving and px_since_spawn >= MIN_GAP_PX:
            parcels.append(Parcel(float(-rng.randint(10, 60))))
            px_since_spawn = 0.0

        frame = bg.copy()

        for p in parcels:
            p.x += dx
            x, y = int(p.x), int(p.y)
            if x + p.w > 0 and x < W and y + p.h > 0 and y < H:
                x0, y0 = max(0, -x), max(0, -y)
                x1, y1 = min(p.w, W - x), min(p.h, H - y)
                roi = frame[y + y0:y + y1, x + x0:x + x1]
                spr = p.sprite[y0:y1, x0:x1]
                a = spr[:, :, 3:4].astype(np.float32) / 255.0
                roi[:] = (spr[:, :, :3].astype(np.float32) * a
                          + roi.astype(np.float32) * (1 - a)).astype(np.uint8)
        parcels = [p for p in parcels if not p.offscreen()]

        draw_banner(frame, i * dt)

        writer.write(frame)

    writer.release()
    print(f"OK: {out_path} ({duration:.0f}s, {total_frames} frames)")


if __name__ == "__main__":
    main()
