"""AWB management: waiting-queue insert, FIFO pairing with parcel idx.

Concept flow: the operator inserts AWB numbers BEFORE parcels enter the
X-ray. Parcels that finish as NEW get the next sequential idx and are paired
with the next WAITING AWB (ordered by created_at) inside the SAME database
transaction as the idx assignment — so an AWB can never drift out of order
relative to parcel idx. Pairing lives in the stores (see
``IdentityStore.finalize_new`` / ``promote_to_new``).
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..dedup.store import AwbRecord, IdentityStore


class AwbPairedError(RuntimeError):
    """AWB already paired to a parcel and cannot be deleted."""


logger = logging.getLogger(__name__)


class AwbService:
    def __init__(self, settings: Settings, store: IdentityStore) -> None:
        self._settings = settings
        self._store = store
        # A single server worker serializes QR check-and-create per warehouse.
        self.qr_locks = {"bgd": asyncio.Lock(), "cas": asyncio.Lock()}

    async def insert(
        self,
        awb_number: str,
        batch_id: str | None = None,
        hawb_number: str | None = None,
        part: int | None = None,
        total: int | None = None,
        *,
        qr_scanned: bool = False,
    ) -> AwbRecord:
        record = await self._store.insert_awb(
            awb_number.strip(), batch_id, hawb_number, part, total, qr_scanned=qr_scanned
        )
        logger.info("AWB inserted: %s (id=%d)", record.awb_number, record.id)
        return record

    async def waiting(self) -> list[AwbRecord]:
        return await self._store.list_awbs("WAITING")

    async def all(self, status: str | None = None) -> list[AwbRecord]:
        return await self._store.list_awbs(status)

    async def delete(self, awb_id: int, *, force: bool = False) -> bool:
        awbs = await self._store.list_awbs()
        target = next((a for a in awbs if a.id == awb_id), None)
        if target is None:
            return False
        if target.status == "PAIRED" and not force:
            raise AwbPairedError(
                f"AWB {target.awb_number!r} sudah terpasang ke paket (id {target.parcel_id}). "
                "Hapus dengan force=true untuk melepas ikatan (paket jadi tanpa AWB, tercatat di audit)."
            )
        return await self._store.delete_awb(awb_id, force=force)
