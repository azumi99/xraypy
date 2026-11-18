"""Review/resolve endpoints for parcels in the gray zone."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..events import EventBus
from ..security import require_operator
from .store import IdentityRecord, IdentityStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dedup", tags=["dedup"])


class ResolveBody(BaseModel):
    action: Literal["NOT_DUPLICATE", "CONFIRM_DUPLICATE", "RESCAN_INTENTIONAL"]
    operator: str = "operator"
    reason: str | None = None


def _snapshot(record: IdentityRecord, counterpart: IdentityRecord | None) -> dict[str, Any]:
    return {
        "id": record.id,
        "batch_id": record.batch_id,
        "track_id": record.track_id,
        "idx": record.idx,
        "status": record.status,
        "review_reason": record.review_reason,
        "sim_score": record.sim_score,
        "crop_ref": record.crop_ref,
        "crop_ref_side": record.crop_ref_side,
        "awb_number": record.awb_number,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "counterpart": (
            {
                "id": counterpart.id,
                "idx": counterpart.idx,
                "crop_ref": counterpart.crop_ref,
                "awb_number": counterpart.awb_number,
            }
            if counterpart
            else None
        ),
    }


@router.get("/review")
async def list_reviews(
    request: Request,
    _: None = Depends(require_operator),
) -> dict[str, Any]:
    store: IdentityStore = request.app.state.store
    pending = await store.pending_reviews()
    items = []
    for rec in pending:
        counterpart = await store.confirm_duplicate_target(rec.id)
        items.append(_snapshot(rec, counterpart))
    return {"items": items}


@router.post("/{identity_id}/resolve")
async def resolve_review(
    identity_id: int,
    body: ResolveBody,
    request: Request,
    _: None = Depends(require_operator),
) -> dict[str, Any]:
    service = request.app.state.dedup
    from ..live import live_resources
    bus: EventBus = live_resources(request.app).bus
    try:
        record, awb = await service.resolve_review(identity_id, body.action, body.operator, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="identity not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    counterpart = await service.store.confirm_duplicate_target(identity_id)
    event_payload = _snapshot(record, counterpart)
    await bus.publish(
        "parcel.review_resolved",
        {**event_payload, "action": body.action, "awb_number": awb.awb_number if awb else record.awb_number},
    )
    logger.info(
        "review %d resolved: %s by %s -> status=%s idx=%s awb=%s",
        identity_id,
        body.action,
        body.operator,
        record.status,
        record.idx,
        record.awb_number,
    )
    return event_payload
