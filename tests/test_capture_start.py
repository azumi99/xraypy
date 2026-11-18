from unittest.mock import MagicMock

from app.capture import Cv2FrameSource
from app.config import CaptureConfig


def test_start_does_not_reopen_a_running_camera(monkeypatch):
    source = Cv2FrameSource(CaptureConfig())
    source._thread = MagicMock()
    source._thread.is_alive.return_value = True
    previous = object()
    source._cap = previous
    opener = MagicMock()
    monkeypatch.setattr("app.capture.cv2.VideoCapture", opener)
    source.start()
    opener.assert_not_called()
    assert source._cap is previous
