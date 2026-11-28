"""Identity store access — pgvector (primary) and sqlite+numpy (dev/fallback).

Both implement the same ``IdentityStore`` interface. Vector comparison is
ALWAYS restricted to rows with the active ``model_name``/``model_version``
so embeddings from different models are never mixed.

idx assignment: only NEW parcels get an idx. The pgvector store takes a
transaction-scoped advisory lock per batch (multi-instance safe); the sqlite
store uses a process lock (single instance). No gaps: DUPLICATE/REVIEW rows
keep idx NULL and never consume a number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from ..config import DedupConfig, Settings, XrayMachineConfig
from .shape_filter import ShapeFeatures, candidates_match

logger = logging.getLogger(__name__)


async def import_legacy_xray_devices(store: IdentityStore, directory: Path) -> None:
    for warehouse in ("cas", "bgd"):
        if await store.get_xray_device(warehouse) is not None:
            continue
        try:
            data = await asyncio.to_thread((directory / f"{warehouse}.json").read_bytes)
            config = XrayMachineConfig.model_validate_json(data)
        except FileNotFoundError:
            continue
        except (OSError, ValidationError):
            logger.warning("Could not import local X-ray device settings for %s", warehouse)
            continue
        await store.save_xray_device(warehouse, config.model_dump_json(by_alias=True), overwrite=False)


ACTIONS_REVIEW = "NOT_DUPLICATE", "CONFIRM_DUPLICATE", "RESCAN_INTENTIONAL"


@dataclass(slots=True)
class IdentityRecord:
    id: int
    batch_id: str
    track_id: int
    status: str  # NEW | REVIEW | DUPLICATE
    idx: int | None = None
    duplicate_of: int | None = None
    sim_score: float | None = None
    review_reason: str | None = None
    model_name: str = ""
    model_version: str = ""
    area: float | None = None
    aspect_ratio: float | None = None
    hu_moments: list[float] = field(default_factory=list)
    crop_ref: str | None = None
    crop_ref_side: str | None = None
    awb_id: int | None = None
    awb_number: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class AwbRecord:
    id: int
    awb_number: str
    batch_id: str | None
    status: str  # WAITING | PAIRED | CANCELLED
    parcel_id: int | None
    created_at: datetime | None = None
    hawb_number: str | None = None
    part: int | None = None
    total: int | None = None
    label_photo_ref: str | None = None
    timecreated: datetime | str | None = None


@dataclass(slots=True)
class SimilarMatch:
    record: IdentityRecord
    similarity: float
    orientation: int


class IdentityStore(ABC):
    """Async interface shared by pgvector and sqlite backends."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_xray_device(self, warehouse: str) -> str | None: ...

    @abstractmethod
    async def save_xray_device(self, warehouse: str, config: str, *, overwrite: bool = True) -> None: ...

    # -- parcels ----------------------------------------------------------
    @abstractmethod
    async def get_by_track(self, batch_id: str, track_id: int) -> IdentityRecord | None: ...

    @abstractmethod
    async def get(self, identity_id: int) -> IdentityRecord | None: ...

    @abstractmethod
    async def get_many_metadata(self, identity_ids: list[int]) -> dict[int, IdentityRecord]:
        """Fetch parcel display metadata in the current warehouse."""

    @abstractmethod
    async def photo_reference_exists(self, ref: str) -> bool:
        """Check whether a photo reference belongs to the current warehouse."""

    @abstractmethod
    async def photo_references_existing(self, refs: list[str]) -> set[str]:
        """Find referenced photos in the current warehouse using a single query."""

    @abstractmethod
    async def list_photo_references(self) -> list[str]:
        """List photo references in the current warehouse for recovery."""

    async def photo_recovery_metadata(self, ref: str):
        """Return the unique parcel view and paired AWB for a referenced photo."""
        raise NotImplementedError

    @abstractmethod
    async def find_similar(
        self,
        embedding: np.ndarray,
        *,
        model_name: str,
        model_version: str,
        batch_id: str,
        exclude_id: int | None = None,
        shape: ShapeFeatures | None = None,
    ) -> list[SimilarMatch]:
        """Top-K nearest stored parcels (active model only, scope-aware,
        shape-prefiltered), best-first."""

    @abstractmethod
    async def insert_identity(
        self,
        *,
        batch_id: str,
        track_id: int,
        status: str,
        embedding: np.ndarray | None,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim_score: float | None = None,
        duplicate_of: int | None = None,
        review_reason: str | None = None,
    ) -> IdentityRecord:
        """Insert without idx (REVIEW / DUPLICATE). embedding may be None when
        inference failed (stored as NULL; searches skip NULL rows)."""

    @abstractmethod
    async def finalize_new(
        self,
        *,
        batch_id: str,
        track_id: int,
        embedding: np.ndarray,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim_score: float | None,
        require_awb: bool,
        crop_ref_side: str | None = None,
        target_awb_id: int | None = None,
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        """Insert NEW and pair the selected AWB, or the next waiting AWB in FIFO mode."""

    @abstractmethod
    async def promote_to_new(
        self, identity_id: int, *, require_awb: bool
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        """REVIEW -> NEW with the next idx (review resolution path)."""

    @abstractmethod
    async def resolve(
        self, identity_id: int, action: str, operator: str, reason: str | None
    ) -> IdentityRecord | None:
        """CONFIRM_DUPLICATE / RESCAN_INTENTIONAL (NOT_DUPLICATE -> promote_to_new)."""

    @abstractmethod
    async def pending_reviews(self) -> list[IdentityRecord]: ...

    @abstractmethod
    async def confirm_duplicate_target(self, identity_id: int) -> IdentityRecord | None:
        """Best stored match for a REVIEW parcel being confirmed as duplicate."""

    @abstractmethod
    async def add_side_capture(
        self,
        identity_id: int,
        embedding: np.ndarray | None,
        crop_ref: str,
        *,
        model_name: str,
        model_version: str,
    ) -> None:
        """Attach the second-POV (side) crop + its embedding (orientation=1).
        The variant participates in duplicate search automatically."""

    @abstractmethod
    async def list_audit(self, identity_id: int) -> list[dict]:
        """Audit rows for one parcel, newest first."""

    @abstractmethod
    async def insert_audit(
        self,
        identity_id: int,
        action: str,
        operator: str,
        reason: str | None,
        sim_score: float | None,
        model_version: str | None,
    ) -> None: ...

    @abstractmethod
    async def last_idx(self, batch_id: str) -> int: ...

    # -- AWBs -------------------------------------------------------------
    @abstractmethod
    async def label_mawbs(self, hawb_number: str) -> list[str]:
        """Distinct MAWBs for an exact HAWB in the current warehouse's labels."""

    @abstractmethod
    async def insert_awb(
        self,
        awb_number: str,
        batch_id: str | None = None,
        hawb_number: str | None = None,
        part: int | None = None,
        total: int | None = None,
        *,
        qr_scanned: bool = False,
    ) -> AwbRecord: ...

    @abstractmethod
    async def list_awbs(self, status: str | None = None) -> list[AwbRecord]: ...

    @abstractmethod
    async def delete_awb(self, awb_id: int, *, force: bool = False) -> bool:
        """Permanently remove an AWB. WAITING/CANCELLED always deletable;
        PAIRED only with force=True (unlinks the parcel first, which becomes
        AWB-less; every unlinked parcel is recorded in review_audit)."""


def make_store(settings: Settings) -> IdentityStore:
    if settings.dedup.store == "pgvector":
        from .pg_store import PgVectorStore

        return PgVectorStore(settings)
    from .sqlite_store import SqliteStore

    return SqliteStore(settings)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def scope_window(cfg: DedupConfig, batch_id: str) -> tuple[str | None, datetime | None]:
    """Returns (batch_filter, since) for the configured dedup scope."""
    if cfg.scope == "batch":
        return batch_id, None
    if cfg.scope == "day":
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return None, today
    return None, None


def filter_candidates_by_shape(
    matches: list[SimilarMatch],
    shape: ShapeFeatures | None,
    cfg: DedupConfig,
) -> list[SimilarMatch]:
    if shape is None:
        return matches
    kept = []
    for m in matches:
        rec = m.record
        if candidates_match(shape, rec.area, rec.aspect_ratio, rec.hu_moments, cfg.shape_filter):
            kept.append(m)
    if not kept and matches:
        logger.debug("shape pre-filter removed all %d vector candidates", len(matches))
    return kept


def hu_from_json(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        return [float(v) for v in json.loads(raw)]
    except (ValueError, TypeError):
        return []


def default_crop_path(crop_dir: Path, batch_id: str, track_id: int) -> Path:
    safe_batch = batch_id.replace("/", "_")
    return crop_dir / safe_batch / f"track_{track_id}.png"


class AwbExistsError(RuntimeError):
    """AWB number already exists in the queue."""


class AwbRequiredError(RuntimeError):
    """NEW parcel but no WAITING AWB and require_for_new is set."""


def new_batch_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
