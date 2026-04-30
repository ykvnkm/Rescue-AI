"""Video frame sources — concrete ``VideoFramePort`` implementations (P1.3).

Five adapters are provided:

* ``FileVideoSource``    — local video file via ``cv2.VideoCapture``.
* ``FolderFramesSource`` — directory of ``*.jpg``/``*.png`` frames.
* ``RTSPVideoSource``    — RTSP stream via ``cv2.VideoCapture`` with reconnect.
* ``FFmpegRTSPSource``   — RTSP stream via an ``ffmpeg`` subprocess (fallback
  when cv2 stalls on high-jitter links).
* ``MjpegHTTPSource``    — HTTP ``multipart/x-mixed-replace`` MJPEG stream.

All adapters satisfy ``rescue_ai.domain.ports.VideoFramePort``. OpenCV,
numpy and ffmpeg are isolated to this layer — the domain sees only
``object`` / ``ndarray`` payloads.
"""

from rescue_ai.infrastructure.video.ffmpeg_rtsp_source import FFmpegRTSPSource
from rescue_ai.infrastructure.video.file_source import FileVideoSource
from rescue_ai.infrastructure.video.folder_source import FolderFramesSource
from rescue_ai.infrastructure.video.mjpeg_http_source import MjpegHTTPSource
from rescue_ai.infrastructure.video.rpi_remote_source import RemoteRpiVideoSource
from rescue_ai.infrastructure.video.rtsp_source import RTSPVideoSource

__all__ = [
    "FFmpegRTSPSource",
    "FileVideoSource",
    "FolderFramesSource",
    "MjpegHTTPSource",
    "RemoteRpiVideoSource",
    "RTSPVideoSource",
]
