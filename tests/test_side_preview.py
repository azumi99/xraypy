from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import numpy as np

from app.config import Settings
from app.pipeline import Pipeline


async def test_track_side_preview_does_not_require_top_frame():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline._annotated = np.ones((5, 5, 3), dtype=np.uint8)
    pipeline._process_side_track_frame = AsyncMock()
    side = np.zeros((100, 100, 3), dtype=np.uint8)
    await pipeline._process_track_frame({"side": side})
    assert pipeline._annotated is None
    pipeline._process_side_track_frame.assert_awaited_once_with(side, ANY, None)


async def test_strip_side_preview_does_not_require_top_frame():
    pipeline = Pipeline(Settings(_env_file=None), MagicMock(), MagicMock(), MagicMock())
    pipeline._annotated = np.ones((5, 5, 3), dtype=np.uint8)
    side = np.full((100, 100, 3), 42, dtype=np.uint8)
    await pipeline._process_strip_frame({"side": side})
    assert pipeline._annotated is None
    assert pipeline._annotated_side is not None
    assert np.all(pipeline._annotated_side == 42)
