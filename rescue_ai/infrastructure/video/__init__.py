"""Video frame sources — concrete ``VideoFramePort`` implementations (P1.3).

Three adapters are provided:

* ``FileVideoSource``  — local video file via ``cv2.VideoCapture``.
* ``FolderFramesSource`` — directory of ``*.jpg``/``*.png`` frames.
* ``RTSPVideoSource`` — RTSP stream with automatic reconnect.

All three satisfy ``rescue_ai.domain.ports.VideoFramePort``. OpenCV and
numpy are isolated to this layer — the domain sees only ``object`` /
``ndarray`` payloads.
"""

from rescue_ai.infrastructure.video.file_source import FileVideoSource
from rescue_ai.infrastructure.video.folder_source import FolderFramesSource
from rescue_ai.infrastructure.video.rtsp_source import RTSPVideoSource

__all__ = [
    "FileVideoSource",
    "FolderFramesSource",
    "RTSPVideoSource",
]
