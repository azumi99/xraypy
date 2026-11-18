"""Photo sync is unavailable until uploads can be verified safely."""

from fastapi import APIRouter, Depends, HTTPException

from .security import require_operator

router = APIRouter(tags=["photos"])


@router.post("/photos/sync")
async def sync_photo(_: None = Depends(require_operator)) -> None:
    raise HTTPException(503, "Photo sync is disabled pending safe photo verification")
