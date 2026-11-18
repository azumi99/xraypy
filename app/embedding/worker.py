"""Out-of-loop embedding inference.

A single worker thread owns the model. The FastAPI event loop submits BGRA
crops via ``submit()`` and awaits an asyncio.Future — heavy torch work never
blocks the WebSocket stream. The queue is bounded: when full, submit raises
``QueueFullError`` and the pipeline marks the parcel REVIEW (backpressure,
never unbounded memory).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

import numpy as np

from ..config import EmbeddingConfig
from .base import Embedder
from .preprocess import preprocess
from .registry import NORMALIZATION

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """Embedding queue at capacity — caller should mark REVIEW."""


class EmbeddingTimeoutError(RuntimeError):
    """Inference did not finish in time — caller should mark REVIEW."""


class EmbeddingWorker:
    def __init__(self, embedder: Embedder, cfg: EmbeddingConfig) -> None:
        self._embedder = embedder
        self._cfg = cfg
        self._queue: queue.Queue[tuple[np.ndarray, asyncio.Future] | None] = queue.Queue(
            maxsize=cfg.queue_max
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def model_name(self) -> str:
        return self._embedder.name

    @property
    def model_version(self) -> str:
        return self._embedder.version

    @property
    def dim(self) -> int:
        return self._embedder.dim

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="embedding-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def submit(self, loop: asyncio.AbstractEventLoop, bgra: np.ndarray) -> asyncio.Future:
        """Queue one crop; returns a Future[np.ndarray] of shape [D]."""
        if self._stop.is_set():
            raise QueueFullError("embedding worker is stopped")
        try:
            fut: asyncio.Future = loop.create_future()
            self._queue.put_nowait((bgra, fut))
        except queue.Full:
            raise QueueFullError(
                f"embedding queue full (max={self._cfg.queue_max}) — backpressure applied"
            ) from None
        return fut

    async def embed(self, bgra: np.ndarray) -> np.ndarray:
        """Await embedding of one crop from the event loop."""
        loop = asyncio.get_running_loop()
        fut = self.submit(loop, bgra)
        return await asyncio.wait_for(fut, timeout=self._cfg.inference_timeout_s)

    def _run(self) -> None:  # worker thread
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            bgra, fut = item
            try:
                vector = self._infer(bgra)
                self._resolve(fut, result=vector)
            except Exception as exc:  # noqa: BLE001 - surface to the awaiting coroutine
                logger.exception("embedding inference failed")
                self._resolve(fut, error=exc)

    def _infer(self, bgra: np.ndarray) -> np.ndarray:
        tensor = preprocess(
            bgra,
            img_size=self._cfg.img_size,
            bg_mode=self._cfg.bg_mode,
            resize_mode=self._cfg.resize_mode,
            mean=NORMALIZATION["mean"],
            std=NORMALIZATION["std"],
        )
        return self._embedder.embed([tensor])[0]

    def _resolve(
        self, fut: asyncio.Future, *, result: np.ndarray | None = None, error: Exception | None = None
    ) -> None:
        loop = fut.get_loop()

        def _set() -> None:
            if fut.cancelled():
                return
            if error is not None:
                fut.set_exception(error)
            else:
                assert result is not None
                fut.set_result(result)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:  # loop closed during shutdown
            pass
