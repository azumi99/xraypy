"""StripTracker — scroll-aware parcel detection for a scrolling X-ray strip.

Screen model (confirmed by operator): parcels that have passed the X-ray
slide to the RIGHT; a new parcel enters from the LEFT edge and nudges the
previous ones rightwards. The shift may be smooth or stepwise — per-frame
phase correlation handles both (a jump is just a large dx, clamped).

Rules:
- Frame 1 is the BASELINE: objects already on screen are NOT captured/indexed.
- A new segment is captured only when it has fully entered (left edge clear
  of the left margin AND a white gap to its left).
- No duplicates within one screen: old objects are the very same pixels
  (tracked by global dx), so only genuinely new segments register.
- Guard rails: low phase-correlation response => skip registration this
  frame; sudden full-strip change (reset/clear button) => re-baseline with
  a warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class KnownObject:
    x1: float
    x2: float
    seq: int  # strip appearance order (display order; NOT the persisted idx)
    baseline: bool = False
    y1: float = 0.0  # tight row bounds (frame coords) — live tracking box
    y2: float = 0.0
    pending: bool = False  # terdeteksi & di-track, belum selesai masuk (belum capture)
    matched_now: bool = False  # segment observed this frame (transient)
    misses: int = 0  # consecutive frames without an observed segment


@dataclass(slots=True)
class NewParcel:
    seq: int
    x1: int
    x2: int
    patch_bgr: np.ndarray  # segment patch (square cut — contour crop happens next)
    y1: int = 0  # tight row bounds (frame coords)
    y2: int = 0


@dataclass
class StripConfig:
    white_thr: int = 200  # column profile: gray >= thr counts as background/gap
    # (HiTraX screen background is ~222 gray, NOT white)
    # white_thr = 0 => AUTO: background estimated per frame (median), objects
    # are pixels deviating from it — works for dark-bg/bright-object videos
    # (screen share) as well as the bright HiTraX background.
    min_gap: int = 6  # consecutive background columns separating objects
    min_w: int = 25  # minimum segment width in px
    left_margin: int = 8  # new object must clear this many px from the left edge
    min_clear_gap: int = 4  # gap required left of a new object before capture
    overlap: float = 0.5  # x-IoU that identifies a segment as a shifted old object
    max_dx: float = 120.0  # clamp for per-frame shift
    min_response: float = 0.15  # phase correlation response floor to trust dx
    reset_similarity: float = 0.35  # mean abs diff above this => strip changed -> re-baseline
    # -- active-row band (auto_band): profiling hanya pada baris yang bergerak.
    # Chrome statis (bar UI pemutar video, bar HiTraX, bodi mesin pada rekaman
    # kamera) tidak pernah ikut profil kolom, jadi tidak menghalangi deteksi.
    auto_band: bool = False
    motion_thr: int = 15  # frame-diff pixel threshold for row activity
    band_min_rows: int = 16  # band lebih kecil => pakai frame penuh
    band_window: int = 60  # rolling window (frames) utk statistik baris aktif
    band_min_ratio: float = 0.1  # baris aktif bila bergerak >= rasio ini dari window


def _auto_bg(gray: np.ndarray) -> tuple[float, float]:
    """Estimate background level + deviation margin from a single frame.

    Background = MODE (most frequent gray) — lebih tahan daripada median saat
    objek+panel menutup lebih dari setengah area. Untuk estimasi lintas-frame
    yang benar-benar stabil (tahan objek mendominasi) lihat
    ``StripTracker._bg_level_and_margin``.
    """
    hist = np.bincount(gray.ravel(), minlength=256)
    top = int(hist.argmax())
    if hist[top] >= gray.size * 0.02:
        bg = float(top)
    else:
        bg = float(np.median(gray))
    mad = float(np.median(np.abs(gray.astype(np.int16) - bg)))
    margin = min(35.0, max(20.0, 4.0 * mad))
    return bg, margin


def _largest_run(mask: np.ndarray) -> tuple[int, int] | None:
    """Longest contiguous True run in a 1-D mask -> (start, end_inclusive)."""
    best: tuple[int, int] | None = None
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if best is None or (i - 1) - start > best[1] - best[0]:
                best = (start, i - 1)
            start = None
    if start is not None:
        if best is None or (len(mask) - 1) - start > best[1] - best[0]:
            best = (start, len(mask) - 1)
    return best


def _bridge_gaps(mask: np.ndarray, max_gap: int = 8) -> np.ndarray:
    """Close gaps of <= max_gap False values between True values (1-D)."""
    out = mask.copy()
    idx = np.flatnonzero(out)
    if idx.size < 2:
        return out
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= max_gap + 1:
            out[a:b] = True
    return out


def _fg_mask(
    roi: np.ndarray, white_thr: int, bg: float | None = None, margin: float | None = None
) -> np.ndarray:
    """2-D foreground mask (objects), magenta overlay pixels excluded."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    b, g, r = roi[:, :, 0].astype(np.int16), roi[:, :, 1].astype(np.int16), roi[:, :, 2].astype(np.int16)
    magenta = (r > 180) & (b > 180) & (g < 100)
    if white_thr > 0:
        fg = gray < white_thr
    elif bg is not None and margin is not None:
        fg = np.abs(gray.astype(np.int16) - bg) > margin
    else:
        bg2, m2 = _auto_bg(gray)
        fg = np.abs(gray.astype(np.int16) - bg2) > m2
    return fg & ~magenta


def _segments_from_fg(
    fg: np.ndarray, min_gap: int, min_w: int, col_mask: np.ndarray | None = None
) -> list[tuple[int, int]]:
    occupied = (fg.sum(axis=0) > 3)
    if col_mask is not None and col_mask.shape == occupied.shape:
        occupied = occupied & col_mask  # kolom statis (area kontrol) = gap
    occupied = occupied.astype(np.int8)
    segs: list[tuple[int, int]] = []
    start: int | None = None
    end, gap = 0, 0
    for x, v in enumerate(occupied):
        if v:
            if start is None:
                start = x
            end, gap = x, 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                if end - start >= min_w:
                    segs.append((start, end))
                start = None
    if start is not None and end - start >= min_w:
        segs.append((start, end))
    return segs


def segment_rows(fg: np.ndarray, a: int, b: int, pad: int = 2) -> tuple[int, int]:
    """Tight row bounds (y1, y2) of foreground inside segment columns a..b."""
    rows = np.flatnonzero(fg[:, a : b + 1].any(axis=1))
    if rows.size == 0:
        return 0, fg.shape[0] - 1
    return int(max(0, rows.min() - pad)), int(min(fg.shape[0] - 1, rows.max() + pad))


def segment_columns(
    roi: np.ndarray, white_thr: int = 235, min_gap: int = 6, min_w: int = 25
) -> list[tuple[int, int]]:
    """Split the strip by column profile: columns without non-white pixels are gaps.

    Pure-magenta UI pixels (if the ROI accidentally includes overlay text) are
    ignored so screen overlays never count as objects. ``white_thr=0`` switches
    to per-frame adaptive background (see ``_auto_bg``).
    """
    return _segments_from_fg(_fg_mask(roi, white_thr), min_gap, min_w)


def estimate_dx(prev_gray: np.ndarray, cur_gray: np.ndarray) -> tuple[float, float]:
    """Global strip shift via phase correlation -> (dx, response)."""
    (dx, _dy), response = cv2.phaseCorrelate(np.float32(prev_gray), np.float32(cur_gray))
    return float(dx), float(response)


def contour_crop_bgra(
    patch_bgr: np.ndarray, *, padding: int = 4, min_side: int = 32, thr: int = 200
) -> tuple[np.ndarray, np.ndarray]:
    """BGRA contour crop inside a segment patch (alpha 0 outside the contour).

    Foreground = pixels darker than ``thr`` (the X-ray background is bright).
    ``thr=0`` => AUTO: Otsu threshold; polarity decided from the patch border
    (the border is mostly background), so bright objects on a dark background
    (screen-share videos) are cropped correctly too.
    """
    gray = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2GRAY)
    if thr > 0:
        _, binary = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY_INV)
    else:
        otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_OTSU)
        border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
        border_med = float(np.median(border))
        dark_cls = gray[gray <= otsu]
        bright_cls = gray[gray > otsu]
        m_dark = float(dark_cls.mean()) if dark_cls.size else 0.0
        m_bright = float(bright_cls.mean()) if bright_cls.size else 255.0
        # background = class paling mirip dengan border patch
        bright_bg = abs(m_bright - border_med) <= abs(m_dark - border_med)
        binary = np.where(gray <= otsu if bright_bg else gray > otsu, 255, 0).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("no contour inside segment patch")
    contour = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(contour)
    x0, y0 = max(0, x - padding), max(0, y - padding)
    x1, y1 = min(patch_bgr.shape[1], x + w + padding), min(patch_bgr.shape[0], y + h + padding)
    if x1 - x0 < min_side or y1 - y0 < min_side:
        raise ValueError(f"contour crop too small: {x1 - x0}x{y1 - y0}")
    patch = patch_bgr[y0:y1, x0:x1]
    alpha = np.zeros(patch.shape[:2], dtype=np.uint8)
    cv2.fillPoly(alpha, [(contour - [x0, y0]).astype(np.int32)], 255)
    return np.dstack([patch, alpha]), contour.reshape(-1, 2) + [x0, y0]


class StripTracker:
    def __init__(self, cfg: StripConfig | None = None) -> None:
        self.cfg = cfg or StripConfig()
        self.known: list[KnownObject] = []
        self._prev_gray: np.ndarray | None = None
        self._next_seq = 1
        self.rebaselined = False  # set True when a sudden change forced re-baseline
        self._row_hits: np.ndarray | None = None  # rolling per-row motion hit counts
        self._moved: np.ndarray = np.zeros((0, 0), dtype=bool)  # pixels that ever moved
        self._band_prev: tuple[int, int] | None = None  # band used by the previous frame
        self._bg_level: float | None = None  # sticky background level (auto mode)
        self._bg_starve = 0  # frames tanpa inlier bg (utk re-init)

    def reset(self) -> None:
        self.known.clear()
        self._prev_gray = None
        self.rebaselined = False
        self._row_hits = None
        self._moved = np.zeros((0, 0), dtype=bool)
        self._band_prev = None
        self._bg_level = None
        self._bg_starve = 0

    # -- sticky background estimate ------------------------------------------------
    def _bg_level_and_margin(self, gray: np.ndarray) -> tuple[float, float]:
        """Level latar lintas-frame: hanya diperbarui dari piksel DEKAT level
        sebelumnya (inlier), jadi objek yang mendominasi layar tak pernah
        menggesernya — mode/median per-frame bisa lompat ke warna objek dan
        melahirkan ghost di celah antar objek."""
        if self._bg_level is None:
            self._bg_level, _ = _auto_bg(gray)
        else:
            dev = np.abs(gray.astype(np.int16) - self._bg_level)
            inlier = gray[dev <= 30]
            if inlier.size >= gray.size * 0.05:
                hist = np.bincount(inlier.ravel(), minlength=256)
                self._bg_level = round(self._bg_level * 0.7 + float(hist.argmax()) * 0.3, 2)
                self._bg_starve = 0
            else:
                self._bg_starve += 1
                if self._bg_starve > 30:  # scene benar-benar berubah -> re-init
                    self._bg_level, _ = _auto_bg(gray)
                    self._bg_starve = 0
        dev = np.abs(gray.astype(np.int16) - self._bg_level)
        inlier = gray[dev <= 30]
        mad = float(np.median(np.abs(inlier.astype(np.int16) - self._bg_level))) if inlier.size else 20.0
        margin = min(35.0, max(20.0, 4.0 * mad))
        return float(self._bg_level), margin

    @property
    def next_seq(self) -> int:
        return self._next_seq

    # -- motion activity (row band + static-pixel mask) ------------------------
    def _update_activity(self, gray: np.ndarray) -> tuple[tuple[int, int] | None, np.ndarray]:
        """Track motion -> (active row band, pixels that ever moved).

        Rows that keep changing form the strip band; static chrome (player UI,
        HiTraX bottom bar, machine body) is excluded from it. Pixels that have
        NEVER changed since the session started are static areas (belt-control
        panel of the X-ray machine, logos, frames) — the returned ``moved``
        mask lets the caller drop them from the foreground so they can never
        be detected as objects. Every pixel swept by a moving object edge
        registers as moved, so passing parcels always stay visible.
        """
        cfg = self.cfg
        h, w = gray.shape
        diff = cv2.absdiff(gray, self._prev_gray)
        moving = diff > cfg.motion_thr
        row_hits = (moving.sum(axis=1) > 3).astype(np.int32)
        if self._row_hits is None or self._row_hits.shape[0] != h or self._moved.shape != (h, w):
            self._row_hits = np.zeros(h, dtype=np.int32)
            self._moved = np.zeros((h, w), dtype=bool)
        self._row_hits = np.minimum(self._row_hits + row_hits, cfg.band_window)
        self._moved |= moving
        thr = max(2, int(cfg.band_window * cfg.band_min_ratio))
        band = None
        active_rows = self._row_hits >= thr
        if active_rows.any():
            run = _largest_run(_bridge_gaps(active_rows))
            if run is not None and (run[1] - run[0] + 1) >= cfg.band_min_rows:
                band = run
        return band, self._moved

    def _baseline_from_segs(
        self, segs: list[tuple[int, int]], fg: np.ndarray, y_off: int, cfg: StripConfig
    ) -> None:
        """Segmen saat (re)baseline: mid-frame = objek lama (baseline), yang
        menempel di tepi kiri sedang MASUK -> pending, tetap di-track live dan
        akan tercapture saat selesai masuk."""
        known: list[KnownObject] = []
        for a, b in segs:
            y1, y2 = segment_rows(fg, a, b)
            if a > cfg.left_margin:
                known.append(KnownObject(float(a), float(b), seq=0, baseline=True, y1=y1 + y_off, y2=y2 + y_off))
            else:
                k = KnownObject(float(a), float(b), seq=self._next_seq, y1=y1 + y_off, y2=y2 + y_off, pending=True)
                self._next_seq += 1
                known.append(k)
        self.known = known

    def update(self, roi_bgr: np.ndarray) -> list[NewParcel]:
        """Process one ROI frame -> parcels that just finished entering."""
        cfg = self.cfg
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        if cfg.auto_band and self._prev_gray is not None:
            band, moved = self._update_activity(gray)
        else:
            band, moved = None, None
        y_off = band[0] if band is not None else 0
        profile_roi = roi_bgr if band is None else roi_bgr[band[0] : band[1] + 1]
        if cfg.white_thr > 0:
            bg = margin = None
        else:
            bg, margin = self._bg_level_and_margin(cv2.cvtColor(profile_roi, cv2.COLOR_BGR2GRAY))
        fg = _fg_mask(profile_roi, cfg.white_thr, bg, margin)
        static = None
        if moved is not None:
            m = moved if band is None else moved[band[0] : band[1] + 1]
            fg = fg & m  # piksel yang tak pernah bergerak (panel kontrol) bukan objek
            static = ~m
        segs = _segments_from_fg(fg, cfg.min_gap, cfg.min_w)

        if self._prev_gray is None:  # first frame after (re)start = baseline
            self._baseline_from_segs(segs, fg, y_off, cfg)
            self._prev_gray = gray
            return []

        if cfg.auto_band and band is None and self._band_prev is None:
            # warm-up: pita aktif belum terbentuk — profil frame penuh belum
            # bersih dari chrome, jangan daftar objek dulu (hanya akumulasi
            # statistik baris). Video tanpa gerak sama sekali tidak pernah
            # lolos dari sini (tidak ada yang di-capture — memang benar).
            self._prev_gray = gray
            return []

        band_just_formed = band is not None and self._band_prev is None
        self._band_prev = band
        if band_just_formed:
            # baseline lama dihitung dari frame penuh (bercampur chrome) —
            # re-baseline dengan profil band yang bersih.
            self._baseline_from_segs(segs, fg, y_off, cfg)
            self._prev_gray = gray
            self.rebaselined = True
            logger.info("active row band formed %s — re-baselining with clean profile", band)
            return []

        dx, response = estimate_dx(self._prev_gray, gray)
        change = float(np.mean(cv2.absdiff(gray, self._prev_gray)))
        self._prev_gray = gray

        # sudden full-strip change (clear button / scene switch) -> re-baseline
        if _strip_changed(gray, self.known, segs, cfg, change):
            logger.warning(
                "strip changed suddenly (diff=%.2f) — re-baselining (old objects stay uncaptured)", change
            )
            self.reset()
            self._baseline_from_segs(segs, fg, y_off, cfg)
            self.rebaselined = True
            return []

        if response < cfg.min_response and abs(dx) > cfg.max_dx / 2:
            logger.warning("strip shift unreliable (dx=%.1f resp=%.2f) — skipping frame", dx, response)
            return []

        dx = float(np.clip(dx, -cfg.max_dx, cfg.max_dx))
        for k in self.known:
            k.x1 += dx
            k.x2 += dx

        # associate segments to known objects and SNAP to the observed bounds —
        # avoids dead-reckoning drift when per-frame dx is slightly biased
        matched_segs: set[int] = set()
        for k in self.known:
            k.matched_now = False
        for k in self.known:
            best_i, best_iou = None, cfg.overlap
            for i, (a, b) in enumerate(segs):
                if i in matched_segs:
                    continue
                iou = _iou_x(a, b, k.x1, k.x2)
                if iou > best_iou:
                    best_i, best_iou = i, iou
            if best_i is not None:
                a, b = segs[best_i]
                k.x1, k.x2 = float(a), float(b)
                y1, y2 = segment_rows(fg, a, b)
                k.y1, k.y2 = float(y1 + y_off), float(y2 + y_off)
                matched_segs.add(best_i)
                k.matched_now, k.misses = True, 0
            else:
                k.misses += 1

        # pending yang tak terlihat lagi (fragmen sesaat / ghost) dibuang
        self.known = [k for k in self.known if not (k.pending and not k.baseline and k.misses > 45)]

        # objects fully out on the right leave the history
        self.known = [k for k in self.known if k.x1 < roi_bgr.shape[1] - 2]

        # pending objects that finished entering (cleared the left margin with
        # a confirmed gap on their left) -> auto capture NOW. Hanya yang
        # segmennya TERAMATI frame ini — koordinat segar, bukan posisi basi.
        new: list[NewParcel] = []
        for k in self.known:
            if not k.pending or k.baseline or not k.matched_now:
                continue
            a = int(k.x1)
            if a > cfg.left_margin and occupied_gap_left(
                profile_roi, a, cfg.white_thr, cfg.min_clear_gap, static, bg, margin
            ):
                k.pending = False
                new.append(
                    NewParcel(
                        k.seq,
                        a,
                        int(k.x2),
                        profile_roi[:, a : int(k.x2) + 1].copy(),
                        y1=int(k.y1),
                        y2=int(k.y2),
                    )
                )

        # brand-new unmatched segments: fully entered + clear gap -> capture
        # immediately; still entering / no gap yet -> live tracking (pending)
        for i, (a, b) in enumerate(segs):
            if i in matched_segs:
                continue  # shifted old object (already snapped)
            # cegah duplikat: bila tumpang tindih dengan known (match IoU gagal
            # karena lompatan besar), snap known itu — jangan buat objek baru
            owner = next((k for k in self.known if _iou_x(a, b, k.x1, k.x2) > 0.15), None)
            if owner is not None:
                owner.x1, owner.x2 = float(a), float(b)
                y1o, y2o = segment_rows(fg, a, b)
                owner.y1, owner.y2 = float(y1o + y_off), float(y2o + y_off)
                owner.matched_now, owner.misses = True, 0
                continue
            y1, y2 = segment_rows(fg, a, b)
            if a > cfg.left_margin and occupied_gap_left(
                profile_roi, a, cfg.white_thr, cfg.min_clear_gap, static, bg, margin
            ):
                k = KnownObject(
                    float(a), float(b), seq=self._next_seq, y1=float(y1 + y_off), y2=float(y2 + y_off)
                )
                self._next_seq += 1
                self.known.append(k)
                new.append(
                    NewParcel(
                        k.seq, a, b, profile_roi[:, a : b + 1].copy(), y1=y1 + y_off, y2=y2 + y_off
                    )
                )
            else:
                # muncul dari kiri (atau belum ada gap) -> track live dulu
                k = KnownObject(
                    float(a),
                    float(b),
                    seq=self._next_seq,
                    y1=float(y1 + y_off),
                    y2=float(y2 + y_off),
                    pending=True,
                )
                self._next_seq += 1
                self.known.append(k)
        return new


def occupied_gap_left(
    roi: np.ndarray,
    seg_start: int,
    white_thr: int,
    min_gap: int,
    static: np.ndarray | None = None,
    bg: float | None = None,
    margin: float | None = None,
) -> bool:
    """True when at least min_gap background columns sit left of the segment."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    lo = max(0, seg_start - min_gap)
    if seg_start - lo < min_gap:
        return False
    cols = gray[:, lo:seg_start]
    if white_thr > 0:
        ok = cols >= white_thr
    else:
        if bg is None or margin is None:
            bg, margin = _auto_bg(gray)
        ok = np.abs(cols.astype(np.int16) - bg) <= margin
    if static is not None and static.shape == gray.shape:
        ok = ok | static[:, lo:seg_start]  # piksel statis (panel) = latar kosong
    clear = ok.all(axis=0) if white_thr > 0 else ok.sum(axis=0) >= cols.shape[0] - 3
    return bool(clear.sum() >= min_gap)


def _strip_changed(
    gray: np.ndarray,
    known: list[KnownObject],
    segs: list[tuple[int, int]],
    cfg: StripConfig,
    change: float,
) -> bool:
    """Heuristic: strip emptied or fully replaced (clear/reset on the scanner)."""
    if not known:
        return False
    expected = len([k for k in known if not k.baseline]) or len(known)
    if len(segs) == 0 and expected > 0 and change > 10.0:
        return True
    return False


def _iou_x(a1: float, a2: float, b1: float, b2: float) -> float:
    inter = max(0.0, min(a2, b2) - max(a1, b1))
    union = max(a2, b2) - min(a1, b1)
    return inter / union if union > 0 else 0.0
