"""WebSocket stream: annotated frames + realtime parcel events.

Message format (JSON):
  {"type": "frame", "data": "<base64 JPEG>"}
  {"type": "parcel.saved" | "parcel.duplicate" | "parcel.review" |
            "parcel.review_resolved" | "session.started" | "session.stopped" |
            "awb.inserted" | "awb.cancelled", ...payload}

Overlay parcel status contract: ``tracking | saved | duplicate | review``;
``index`` is null for duplicate/review (no idx consumed).
"""

from __future__ import annotations

import asyncio
import base64
import logging

import cv2
from fastapi import APIRouter, WebSocket

from ..live import live_resources
from ..security import authorized, identity, require_operator_ws
from ..warehouse import warehouse_context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])

FRAME_INTERVAL_S = 1 / 10  # max 10 fps to the browser


@router.websocket("/ws/push/{view}")
async def push_screen(ws: WebSocket, view: str) -> None:
    """Browser getDisplayMedia pushes JPEG frames here (binary messages)."""
    if not await require_operator_ws(ws):
        return
    if view not in ("top", "side"):
        await ws.close(code=1008)
        return
    user = identity(ws)
    if user is None:
        await ws.close(code=1008)
        return
    token = warehouse_context.set(user["access"])
    await ws.accept()
    mgr = live_resources(ws.app).source_mgr
    try:
        while True:
            data = await ws.receive_bytes()
            if not authorized(ws):
                await ws.close(code=1008)
                return
            await asyncio.to_thread(mgr.push_screen, view, data)
    except Exception:  # noqa: BLE001 — client disconnect ends the loop
        pass
    finally:
        warehouse_context.reset(token)


@router.websocket("/ws/stream")
async def stream(ws: WebSocket) -> None:
    if not await require_operator_ws(ws):
        return
    user = identity(ws)
    if user is None:
        await ws.close(code=1008)
        return
    token = warehouse_context.set(user["access"])
    await ws.accept()
    runtime = live_resources(ws.app)
    bus = runtime.bus
    pipeline = runtime.pipeline
    queue = bus.subscribe()
    try:
        if getattr(getattr(ws.app, "state", None), "awb_waiting_notifications", False):
            await ws.send_json({"type": "awb.waiting_live", "available": True})
        last_frame = 0.0
        while True:
            if not authorized(ws):
                await ws.close(code=1008)
                return
            timeout = max(0.0, last_frame + FRAME_INTERVAL_S - asyncio.get_event_loop().time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                event = None
            now = asyncio.get_event_loop().time()
            if now - last_frame >= FRAME_INTERVAL_S:
                frame = pipeline.latest_annotated() if pipeline is not None else None
                side = pipeline.latest_annotated_side() if pipeline is not None else None
                if frame is not None or side is not None:
                    msg: dict = {
                        "type": "frame",
                        "data": None,
                        "overlays": pipeline.overlay_states() if pipeline else [],
                    }
                    if frame is not None:
                        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        if ok:
                            msg["data"] = base64.b64encode(buf.tobytes()).decode("ascii")
                    if side is not None:
                        ok2, buf2 = cv2.imencode(".jpg", side, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        if ok2:
                            msg["side"] = base64.b64encode(buf2.tobytes()).decode("ascii")
                    await ws.send_json(msg)
                last_frame = now
            if event is not None:
                await ws.send_json(event)
    except Exception as exc:  # noqa: BLE001 — client disconnects land here
        logger.debug("stream closed: %s", exc)
    finally:
        bus.unsubscribe(queue)
        warehouse_context.reset(token)
