"""Operator confirmation for a single track-mode capture per warehouse."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import uuid4

import numpy as np

from .dedup.store import AwbRecord

ARM_TIMEOUT = 25


@dataclass
class CaptureFrames:
    top: np.ndarray
    side: np.ndarray
    captured_at: datetime
    side_captured_at: datetime | None = None

    @property
    def images(self):
        return self.top, self.side


@dataclass
class CaptureSelection:
    awb: AwbRecord | None = None
    state: str = "idle"
    revision: str = field(default_factory=lambda: uuid4().hex)
    reason: str | None = None
    deadline: float = 0
    candidate: CaptureFrames | None = None
    manual_frames: CaptureFrames | None = None
    held: CaptureFrames | None = None

    def move(self, state: str, reason: str | None = None) -> None:
        self.state = state
        self.reason = reason
        self.revision = uuid4().hex

    def arm(self, awb: AwbRecord) -> None:
        self.awb = awb
        self.retry()

    def retry(self) -> None:
        self.candidate = self.manual_frames = None
        self.deadline = monotonic() + ARM_TIMEOUT
        self.move("armed")

    def clear(self) -> None:
        self.awb = self.manual_frames = None
        self.candidate, self.held = self.held, None
        self.move("unassigned" if self.candidate is not None else "idle")

    def describe(self) -> dict:
        awb = self.awb
        return {
            "state": self.state,
            "revision": self.revision,
            "reason": self.reason,
            "awb": {"id": awb.id, "hawb": awb.hawb_number, "mawb": awb.awb_number,
                    "part": awb.part, "total": awb.total} if awb else None,
        }
