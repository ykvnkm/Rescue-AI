"""Remote RPi video source: start RTSP stream on RPi, decode locally.

Wraps :class:`RpiClient` — calls ``start_stream`` on construction to
obtain an RTSP URL from the Raspberry Pi source service, then delegates
frame decoding to an inner :class:`RTSPVideoSource`. On ``close()`` the
inner source is released and ``stop_stream(session_id)`` is sent back to
the RPi so the device doesn't leak sessions.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from rescue_ai.infrastructure.rpi_client import RpiClient
from rescue_ai.infrastructure.video.rtsp_source import RTSPVideoSource

logger = logging.getLogger(__name__)


class RemoteRpiVideoSource:
    """``VideoFramePort`` wrapping a remote RPi RTSP session."""

    def __init__(
        self,
        *,
        rpi_client: RpiClient,
        mission_id: str,
        target_fps: float,
    ) -> None:
        if not mission_id:
            raise ValueError("mission_id must be non-empty")
        if target_fps <= 0:
            raise ValueError("target_fps must be positive")
        self._client = rpi_client
        self._mission_id = mission_id
        session = rpi_client.start_stream(
            mission_id=mission_id, target_fps=float(target_fps)
        )
        self._session_id = session.session_id
        self._rtsp_url = session.rtsp_url
        logger.info(
            "RemoteRpiVideoSource started: mission=%s session=%s url=%s",
            mission_id,
            session.session_id,
            session.rtsp_url,
        )
        self._inner = RTSPVideoSource(session.rtsp_url)
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def rtsp_url(self) -> str:
        return self._rtsp_url

    def session_stats(self) -> dict[str, object]:
        """Fetch live RPi session stats (FPS, dropped frames, etc.).

        The :class:`AutoSession` polls this via duck-typing every ~2s and
        embeds the payload in WebSocket frame events so the UI can show
        RPi-side counters in stream mode.
        """
        if not self._session_id:
            return {}
        try:
            return self._client.session_stats(self._session_id)
        except (RuntimeError, ValueError, OSError) as error:
            logger.debug("RemoteRpiVideoSource: session_stats failed: %s", error)
            return {}

    def frames(self) -> Iterator[tuple[object, float, int]]:
        return self._inner.frames()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._inner.close()
        finally:
            if self._session_id:
                try:
                    self._client.stop_stream(self._session_id)
                except (RuntimeError, ValueError, OSError):  # pragma: no cover
                    logger.exception(
                        "RemoteRpiVideoSource: stop_stream failed for session=%s",
                        self._session_id,
                    )


__all__ = ["RemoteRpiVideoSource"]
