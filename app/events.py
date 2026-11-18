"""Simple async event bus — WebSocket clients subscribe, services publish."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"type": event_type, **payload}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("event subscriber queue full — dropping %s event", event_type)

    def publish_nowait(self, event_type: str, payload: dict[str, Any]) -> None:
        import asyncio as _a

        try:
            loop = _a.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish(event_type, payload))
