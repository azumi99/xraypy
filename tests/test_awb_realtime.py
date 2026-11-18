import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import numpy as np

from app.events import EventBus
from app.ws import stream as ws_module


async def test_awb_event_delivered_without_video_frames(monkeypatch):
    bus = EventBus()
    original_subscribe = bus.subscribe

    def subscribe():
        queue = original_subscribe()
        queue.put_nowait({"type": "awb.inserted", "awb": {"id": 1}})
        return queue

    monkeypatch.setattr(bus, "subscribe", subscribe)
    monkeypatch.setattr(ws_module, "require_operator_ws", AsyncMock(return_value=True))
    monkeypatch.setattr(ws_module, "identity", lambda _: {"access": "bgd"})
    monkeypatch.setattr(ws_module, "authorized", lambda _: True)
    pipeline = SimpleNamespace(latest_annotated=lambda: None, latest_annotated_side=lambda: None)
    monkeypatch.setattr(ws_module, "live_resources", lambda _: SimpleNamespace(bus=bus, pipeline=pipeline))
    sent = []

    async def send_json(event):
        sent.append(event)
        raise RuntimeError("client disconnected")

    socket = SimpleNamespace(app=object(), accept=AsyncMock(), send_json=send_json)
    await asyncio.wait_for(ws_module.stream(socket), timeout=1)
    assert sent == [{"type": "awb.inserted", "awb": {"id": 1}}]


async def test_side_frame_is_streamed_without_top_frame(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(ws_module, "require_operator_ws", AsyncMock(return_value=True))
    monkeypatch.setattr(ws_module, "identity", lambda _: {"access": "bgd"})
    monkeypatch.setattr(ws_module, "authorized", lambda _: True)
    side = np.full((8, 12, 3), 123, dtype=np.uint8)
    pipeline = SimpleNamespace(latest_annotated=lambda: None, latest_annotated_side=lambda: side,
                               overlay_states=lambda: [])
    monkeypatch.setattr(ws_module, "live_resources", lambda _: SimpleNamespace(bus=bus, pipeline=pipeline))
    sent = []

    async def send_json(event):
        sent.append(event)
        raise RuntimeError("client disconnected")

    socket = SimpleNamespace(app=object(), accept=AsyncMock(), send_json=send_json)
    await asyncio.wait_for(ws_module.stream(socket), timeout=1)
    assert sent[0]["type"] == "frame" and sent[0]["data"] is None
    decoded = cv2.imdecode(np.frombuffer(base64.b64decode(sent[0]["side"]), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == side.shape
