"""Application configuration (pydantic-settings).

Every tunable lives here; nothing is hardcoded in the modules.
Secrets come from environment variables only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingModelName = Literal["resnet18", "resnet50", "dinov2_small", "dinov2_base"]
BgMode = Literal["black", "keep", "mean"]
ResizeMode = Literal["fill", "contain", "cover"]
DedupScope = Literal["batch", "day", "all"]
StoreBackend = Literal["pgvector", "sqlite"]
DetectorBackend = Literal["yolo", "motion"]
CaptureSourceKind = Literal["device", "file", "synthetic"]


class EmbeddingConfig(BaseModel):
    model: EmbeddingModelName = "dinov2_small"
    pooling: Literal["cls", "mean", "cls_mean"] = "cls"
    device: str = "cpu"
    half: bool = False
    bg_mode: BgMode = "black"
    resize_mode: ResizeMode = "fill"
    img_size: int = 224
    multi_orientation: bool = False
    queue_max: int = 50
    inference_timeout_s: float = 10.0


class ShapeFilterConfig(BaseModel):
    area_tol: float = 0.25
    aspect_tol: float = 0.15
    hu_max_dist: float = 0.5


class DedupConfig(BaseModel):
    # PROVISIONAL thresholds — must be calibrated with real scanner crops
    # (tools/calibrate_threshold.py) before production use.
    t_high: float = 0.92
    t_low: float = 0.80
    scope: DedupScope = "batch"
    top_k: int = 5
    shape_filter: ShapeFilterConfig = Field(default_factory=ShapeFilterConfig)
    store: StoreBackend = "pgvector"


class HnswConfig(BaseModel):
    m: int = 16
    ef_construction: int = 64


class DbConfig(BaseModel):
    dsn_env: str = "CV_DATABASE_URL"
    hnsw: HnswConfig = Field(default_factory=HnswConfig)


class CaptureConfig(BaseModel):
    source_kind: CaptureSourceKind = "synthetic"
    # device index for HDMI capture card (e.g. "0" for cv2.VideoCapture(0)),
    # or a video file path when source_kind=file.
    source: str = "0"
    width: int = 1280
    height: int = 720
    fps: int = 30
    # per-POV source: "screen" (share dari browser) | "camera:<index>" | "synthetic"
    # (lapangan: tampak atas & samping masing-masing dari 1 kamera HDMI capture)
    top_source: str = "screen"
    side_source: str = "screen"
    # Side capture time minus top capture time, in seconds; calibrate per installation.
    pairing_side_delay_s: float = Field(default=0.0, allow_inf_nan=False)
    pairing_max_skew_s: float = Field(default=0.5, gt=0, allow_inf_nan=False)


class StripConfigModel(BaseModel):
    # ROI of the X-ray strip inside the captured frame (pixels). Defaults tuned
    # on the HiTraX sample (640x360): strip area y<308, x in [2, 638); the
    # bottom UI bar (HiTraX / clock / thumbnails) is excluded.
    roi_x: int = 2
    roi_y: int = 0
    roi_w: int = 636  # 0 => frame width - roi_x
    roi_h: int = 308  # 0 => frame height - roi_y
    # column-profile segmentation. Background of the HiTraX screen is ~222
    # gray, so "occupied" means darker than 200 (NOT white-paged 235).
    # 0 => AUTO: background estimated per frame (median + MAD margin) — objects
    # may be darker OR brighter than the background (screen-share videos).
    white_thr: int = 200
    min_gap: int = 6
    min_w: int = 25
    left_margin: int = 8
    min_clear_gap: int = 4
    overlap: float = 0.5
    # phase-correlation guards
    max_dx: float = 120.0
    min_response: float = 0.15
    # pita baris aktif: profiling hanya pada baris yang bergerak — chrome
    # statis (bar UI pemutar video / HiTraX / bodi mesin) tereksklusi otomatis.
    auto_band: bool = False
    motion_thr: int = 15
    band_min_rows: int = 16
    band_window: int = 60
    band_min_ratio: float = 0.1
    # contour crop inside each segment: foreground = gray < crop_thr
    # 0 => AUTO (Otsu, polarity dari warna border patch)
    crop_thr: int = 200
    min_crop_side_px: int = 32


PipelineMode = Literal["strip", "track"]


class DetectorConfig(BaseModel):
    backend: DetectorBackend = "motion"
    # yolo backend — YOLO26-seg (terbaru) + ByteTrack. Untuk produksi X-ray,
    # fine-tune model parcel lalu set weights + classes, mis:
    #   CV_DETECTOR__WEIGHTS=parcel_yolo12s.pt  CV_DETECTOR__CLASSES=parcel
    weights: str = "yolo26n-seg.pt"
    conf: float = 0.3
    iou: float = 0.5
    # filter kelas: "all" (class-agnostic) atau daftar dipisah koma, mis
    # "suitcase,backpack,handbag" (COCO) atau "parcel" (model custom).
    classes: str = "all"
    # track stabil seberapa lama sebelum di-capture (frame)
    min_track_hits: int = 5
    # motion backend
    bg_history: int = 300
    min_area_px: int = 2500
    # tracker
    finalize_missed_frames: int = 12
    min_crop_side_px: int = 32
    # Optional exclusive bottom of the scanner image, as a fraction of frame height.
    viewport_bottom: float | None = Field(default=None, gt=0, le=1)


class AwbConfig(BaseModel):
    # AWB numbers inserted here go into the waiting queue; pairing is FIFO by
    # created_at within the session batch.
    require_for_new: bool = False  # if True, NEW parcel without waiting AWB -> REVIEW


class PhotoSyncConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""  # mis. https://example.com
    interval_s: float = 15.0



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CV_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://ilhambintang@localhost:5432/object_cv"
    # only used when dedup.store=sqlite (dev/test fallback)
    sqlite_path: str = "data/cv.sqlite"
    host: str = "127.0.0.1"
    port: int = 8100
    # Shared operator token for WebSocket + review endpoints. None => open
    # access (dev only). Issue one per operator deployment in production.
    stream_token: str | None = None
    login_password: str | None = None
    jwt_secret: str | None = None
    bootstrap_password: str | None = None
    photo_sync_enabled: bool = False
    photo_sync_url: str = ""
    photo_sync_outbox_path: Path = Path("data/photo-sync-outbox.sqlite3")
    photo_sync_bgd_token: str = ""
    photo_sync_cas_token: str = ""
    photo_sync_devices_json: str = ""
    # Physical camera indices must be explicitly assigned to one warehouse.
    warehouse_bgd_cameras: str = ""
    warehouse_cas_cameras: str = ""
    crop_dir: Path = Path("data/crops")
    photo_sync: PhotoSyncConfig = Field(default_factory=PhotoSyncConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    strip: StripConfigModel = Field(default_factory=StripConfigModel)
    pipeline_mode: PipelineMode = "strip"  # strip (default) | track (YOLO/ByteTrack alt.)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    awb: AwbConfig = Field(default_factory=AwbConfig)


def load_settings() -> Settings:
    return Settings()
