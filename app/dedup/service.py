"""Duplicate detection service — the NEW / REVIEW / DUPLICATE decision.

Safe bias: when in doubt choose REVIEW over DUPLICATE. Any embedding/store
failure degrades to REVIEW with an explicit reason; the pipeline never stops
and no parcel is silently dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..config import Settings
from ..embedding.worker import EmbeddingTimeoutError, EmbeddingWorker, QueueFullError
from .shape_filter import ShapeFeatures, compute_shape_features
from .store import AwbRecord, IdentityRecord, IdentityStore, SimilarMatch

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Decision:
    status: str  # NEW | REVIEW | DUPLICATE
    record: IdentityRecord
    awb: AwbRecord | None = None
    top_match: SimilarMatch | None = None
    reason: str | None = None


class DedupService:
    def __init__(self, settings: Settings, worker: EmbeddingWorker, store: IdentityStore) -> None:
        self._settings = settings
        self._worker = worker
        self._store = store

    @property
    def store(self) -> IdentityStore:
        return self._store

    @property
    def worker(self) -> EmbeddingWorker:
        return self._worker

    async def process_parcel(
        self,
        *,
        batch_id: str,
        track_id: int,
        crop_bgra: np.ndarray,
        crop_ref: str | None = None,
        shape: ShapeFeatures | None = None,
    ) -> Decision:
        """Full decision flow for one finalized parcel crop."""
        if shape is None:
            shape = compute_shape_features(crop_bgra)
        if (
            shape.area <= 0
            or min(crop_bgra.shape[0], crop_bgra.shape[1]) < self._settings.detector.min_crop_side_px
        ):
            return await self._review(
                batch_id=batch_id,
                track_id=track_id,
                crop_bgra=crop_bgra,
                crop_ref=crop_ref,
                shape=shape,
                reason="CROP_INVALID",
                detail="crop too small or empty mask",
            )
        try:
            embedding = await self._worker.embed(crop_bgra)
        except EmbeddingTimeoutError:
            logger.warning("embedding timeout for track %s — REVIEW", track_id)
            return await self._review(batch_id, track_id, crop_bgra, crop_ref, shape, "EMBEDDING_TIMEOUT")
        except QueueFullError as exc:
            logger.warning("embedding queue full for track %s — REVIEW (%s)", track_id, exc)
            return await self._review(batch_id, track_id, crop_bgra, crop_ref, shape, "QUEUE_FULL")
        except Exception as exc:  # noqa: BLE001 — never stop the pipeline
            logger.exception("embedding failed for track %s — REVIEW", track_id)
            return await self._review(
                batch_id, track_id, crop_bgra, crop_ref, shape, "EMBEDDING_FAILED", detail=str(exc)
            )

        model_name, model_version = self._worker.model_name, self._worker.model_version

        # idempotency: same (batch, track) processed again -> return same result
        existing = await self._store.get_by_track(batch_id, track_id)
        if existing is not None:
            logger.info(
                "duplicate processing of (batch=%s track=%s), returning existing %s",
                batch_id,
                track_id,
                existing.status,
            )
            top = await self._best_match_for(existing)
            return Decision(existing.status, existing, top_match=top, reason=existing.review_reason)

        try:
            matches = await self._store.find_similar(
                embedding,
                model_name=model_name,
                model_version=model_version,
                batch_id=batch_id,
                shape=shape,
            )
        except Exception as exc:  # noqa: BLE001 — store down -> degrade to REVIEW
            logger.error("identity store search failed — REVIEW fallback: %s", exc)
            return await self._review(
                batch_id, track_id, crop_bgra, crop_ref, shape, "STORE_UNAVAILABLE", detail=str(exc)
            )

        top: SimilarMatch | None = matches[0] if matches else None
        sim = top.similarity if top else None
        cfg = self._settings.dedup

        if sim is not None and sim >= cfg.t_high:
            return await self._mark_duplicate(
                batch_id=batch_id,
                track_id=track_id,
                embedding=embedding,
                model_name=model_name,
                model_version=model_version,
                shape=shape,
                crop_ref=crop_ref,
                sim=sim,
                duplicate_of=top.record.id,
            )
        if sim is not None and sim >= cfg.t_low:
            return await self._review(
                batch_id,
                track_id,
                crop_bgra,
                crop_ref,
                shape,
                "GRAY_ZONE",
                detail=f"sim={sim:.3f} in [{cfg.t_low}, {cfg.t_high})",
                embedding=embedding,
                model_name=model_name,
                model_version=model_version,
                sim_score=sim,
                duplicate_of=top.record.id if top else None,
            )
        # NEW
        try:
            record, awb = await self._store.finalize_new(
                batch_id=batch_id,
                track_id=track_id,
                embedding=embedding,
                model_name=model_name,
                model_version=model_version,
                shape=shape,
                crop_ref=crop_ref,
                sim_score=sim,
                require_awb=self._settings.awb.require_for_new,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to finalize NEW parcel: %s", exc)
            return await self._review(
                batch_id, track_id, crop_bgra, crop_ref, shape, "STORE_UNAVAILABLE", detail=str(exc)
            )
        return Decision("NEW", record, awb=awb, top_match=top)

    # -- helpers -----------------------------------------------------------
    async def _review(
        self,
        batch_id: str,
        track_id: int,
        crop_bgra: np.ndarray,
        crop_ref: str | None,
        shape: ShapeFeatures,
        reason: str,
        detail: str | None = None,
        *,
        embedding: np.ndarray | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        sim_score: float | None = None,
        duplicate_of: int | None = None,
    ) -> Decision:
        existing = await self._store.get_by_track(batch_id, track_id)
        if existing is not None:
            return Decision(existing.status, existing, reason=existing.review_reason)
        # embedding is None => no embedding available: store NULL — searches skip NULL rows.
        model_name = model_name or self._worker.model_name
        model_version = model_version or self._worker.model_version
        record = await self._store.insert_identity(
            batch_id=batch_id,
            track_id=track_id,
            status="REVIEW",
            embedding=embedding,
            model_name=model_name,
            model_version=model_version,
            shape=shape,
            crop_ref=crop_ref,
            sim_score=sim_score,
            duplicate_of=duplicate_of,
            review_reason=reason,
        )
        await self._store.insert_audit(
            record.id, f"AUTO_REVIEW_{reason}", "system", detail, sim_score, model_version
        )
        logger.info("parcel batch=%s track=%s -> REVIEW (%s %s)", batch_id, track_id, reason, detail or "")
        return Decision("REVIEW", record, reason=reason)

    async def _mark_duplicate(
        self,
        *,
        batch_id: str,
        track_id: int,
        embedding: np.ndarray,
        model_name: str,
        model_version: str,
        shape: ShapeFeatures,
        crop_ref: str | None,
        sim: float,
        duplicate_of: int,
    ) -> Decision:
        existing = await self._store.get_by_track(batch_id, track_id)
        if existing is not None:
            return Decision(existing.status, existing, reason=existing.review_reason)
        record = await self._store.insert_identity(
            batch_id=batch_id,
            track_id=track_id,
            status="DUPLICATE",
            embedding=embedding,
            model_name=model_name,
            model_version=model_version,
            shape=shape,
            crop_ref=crop_ref,
            sim_score=sim,
            duplicate_of=duplicate_of,
        )
        await self._store.insert_audit(
            record.id, "AUTO_DUPLICATE", "system", f"sim={sim:.4f}", sim, model_version
        )
        logger.info(
            "parcel batch=%s track=%s -> DUPLICATE of %d (sim=%.3f)", batch_id, track_id, duplicate_of, sim
        )
        top = SimilarMatch(await self._store.get(duplicate_of), sim, 0)  # type: ignore[arg-type]
        return Decision("DUPLICATE", record, top_match=top)

    async def _best_match_for(self, record: IdentityRecord) -> SimilarMatch | None:
        if record.duplicate_of is None:
            return None
        target = await self._store.get(record.duplicate_of)
        if target is None or record.sim_score is None:
            return None
        return SimilarMatch(target, record.sim_score, 0)

    async def resolve_review(
        self, identity_id: int, action: str, operator: str, reason: str | None = None
    ) -> tuple[IdentityRecord, AwbRecord | None]:
        """Operator resolution of a REVIEW parcel (see dedup/review.py)."""
        if action == "NOT_DUPLICATE":
            record, awb = await self._store.promote_to_new(
                identity_id, require_awb=self._settings.awb.require_for_new
            )
            return record, awb
        record = await self._store.resolve(identity_id, action, operator, reason)
        if record is None:
            raise KeyError(f"identity {identity_id} not found")
        return record, None
