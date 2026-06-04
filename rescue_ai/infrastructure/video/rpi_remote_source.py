"""Remote RPi video source: start a stream on the RPi, decode locally.

Wraps :class:`RpiClient` — calls ``start_stream`` on construction to obtain
the device's stream coordinates, then decodes frames locally. The primary
transport is RTSP (low-latency, the device's native channel). If RTSP cannot
deliver frames the source transparently falls back to the HTTP MJPEG endpoint
the RPi advertises in the same ``start_stream`` response (``stream_url``),
reusing the RPi link's mTLS material. On ``close()`` the active decoder is
released and ``stop_stream(session_id)`` is sent back so the device doesn't
leak sessions.
"""

from __future__ import annotations

import logging
import ssl
import urllib.request
from collections.abc import Iterator

from rescue_ai.infrastructure.rpi_client import RpiClient
from rescue_ai.infrastructure.video.mjpeg_http_source import (
    HttpStreamLike,
    MjpegHTTPSettings,
    MjpegHTTPSource,
)
from rescue_ai.infrastructure.video.rtsp_source import RTSPVideoSource

logger = logging.getLogger(__name__)

# When an HTTP MJPEG fallback is available, give RTSP a short leash before
# failing over so a non-publishing RTSP server does not stall the mission for
# the full default reconnect budget. With no fallback we keep the default.
_RTSP_ATTEMPTS_WITH_FALLBACK = 4


class RemoteRpiVideoSource:
    """``VideoFramePort`` wrapping a remote RPi stream (RTSP → HTTP MJPEG)."""

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
        self._target_fps = float(target_fps)
        session = rpi_client.start_stream(
            mission_id=mission_id, target_fps=float(target_fps)
        )
        self._session_id = session.session_id
        self._rtsp_url = session.rtsp_url
        self._stream_url = session.stream_url
        logger.info(
            "RemoteRpiVideoSource started: mission=%s session=%s rtsp=%s mjpeg=%s",
            mission_id,
            session.session_id,
            session.rtsp_url or "-",
            session.stream_url or "-",
        )
        if not self._rtsp_url and not self._stream_url:
            raise RuntimeError(
                "RPi start_stream returned neither rtsp_url nor stream_url"
            )
        self._rtsp: RTSPVideoSource | None = None
        if self._rtsp_url:
            self._rtsp = RTSPVideoSource(
                self._rtsp_url,
                max_reconnect_attempts=(
                    _RTSP_ATTEMPTS_WITH_FALLBACK if self._stream_url else 10
                ),
            )
        self._mjpeg: MjpegHTTPSource | None = None
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
        # Primary: RTSP. On connect/read exhaustion, fall back to HTTP MJPEG
        # (the device advertises both in the same start_stream response).
        if self._rtsp is not None:
            try:
                yield from self._rtsp.frames()
                return
            except RuntimeError as error:
                if not self._stream_url:
                    raise
                logger.warning(
                    "RemoteRpiVideoSource: RTSP unavailable (%s); "
                    "falling back to HTTP MJPEG %s",
                    error,
                    self._stream_url,
                )
        self._mjpeg = MjpegHTTPSource(
            self._stream_url,
            settings=MjpegHTTPSettings(http_opener=self._mjpeg_opener()),
        )
        yield from self._mjpeg.frames()

    def _mjpeg_opener(self):
        """Build an HTTP opener that mirrors the RPi link's mTLS material."""
        verify = self._client.tls_verify
        cert = self._client.tls_cert

        def opener(
            url: str, connect_timeout: float, read_timeout: float
        ) -> HttpStreamLike:
            _ = connect_timeout
            context: ssl.SSLContext | None = None
            if cert is not None:
                cafile = verify if isinstance(verify, str) and verify else None
                context = ssl.create_default_context(cafile=cafile)
                context.load_cert_chain(cert[0], cert[1])
                # Station certs are issued to an IP / private CA, not a DNS SAN.
                context.check_hostname = False
            return urllib.request.urlopen(  # noqa: S310
                url, timeout=read_timeout, context=context
            )

        return opener

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._rtsp is not None:
                self._rtsp.close()
            if self._mjpeg is not None:
                self._mjpeg.close()
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
