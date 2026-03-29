"""HTTP client for Raspberry Pi frame source service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

from rescue_ai.config import RpiSettings


@dataclass(frozen=True)
class RpiMissionInfo:
    """Single mission available on the Raspberry Pi."""

    mission_id: str
    name: str
    images_dir: str
    annotations_json: str | None


@dataclass(frozen=True)
class RpiCatalog:
    """Catalog of available missions and videos on RPi."""

    missions: list[RpiMissionInfo]


@dataclass(frozen=True)
class RpiStreamSession:
    """Active RTSP stream session on RPi."""

    session_id: str
    rtsp_url: str


class RpiClient:
    """Communicates with the RPi source service over HTTP."""

    def __init__(self, settings: RpiSettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._missions_dir = settings.missions_dir.strip()
        self._rtsp_port = settings.rtsp_port
        self._rtsp_path_prefix = settings.rtsp_path_prefix

    def health(self, timeout_sec: float = 5.0) -> dict[str, object]:
        """Check RPi service health. Raises on connection failure."""
        response = httpx.get(
            f"{self._base_url}/health",
            timeout=timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def catalog(self, timeout_sec: float = 10.0) -> RpiCatalog:
        """Fetch the mission catalog from RPi."""
        response = httpx.get(
            f"{self._base_url}/mission/catalog",
            timeout=timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        missions = [
            RpiMissionInfo(
                mission_id=item["id"],
                name=item["name"],
                images_dir=item.get("images_dir", ""),
                annotations_json=item.get("annotations_json"),
            )
            for item in data.get("missions", [])
        ]
        return RpiCatalog(missions=missions)

    def start_stream(
        self,
        mission_id: str,
        *,
        mode: str = "frames",
        target_fps: float = 6.0,
        timeout_sec: float = 15.0,
    ) -> RpiStreamSession:
        """Start a streaming session on the RPi."""
        mission_path = self._resolve_mission_path(mission_id=mission_id)
        response = httpx.post(
            f"{self._base_url}/source/start",
            json={
                "mode": mode,
                "source": mission_path,
                "mission_id": mission_id,
                "realtime": True,
                "target_fps": target_fps,
            },
            timeout=timeout_sec,
        )
        if response.status_code == 404:
            raise ValueError(f"RPi mission not found: {mission_id}")
        response.raise_for_status()
        data = response.json()
        session_id = data.get("session_id", "")
        rtsp_host = self._base_url.split("://")[-1].split(":")[0]
        rtsp_url = (
            f"rtsp://{rtsp_host}:{self._rtsp_port}"
            f"/{self._rtsp_path_prefix}/{session_id}"
        )
        return RpiStreamSession(session_id=session_id, rtsp_url=rtsp_url)

    def _resolve_mission_path(self, mission_id: str) -> str:
        mission_name = mission_id.strip()
        if not mission_name:
            raise ValueError("RPi mission id must not be empty")
        root_dir = self._missions_dir
        if not root_dir:
            raise ValueError("RPI_MISSIONS_DIR not configured")
        return str(PurePosixPath(root_dir) / mission_name)

    def stop_stream(
        self, session_id: str, timeout_sec: float = 10.0
    ) -> dict[str, object]:
        """Stop an active streaming session."""
        response = httpx.post(
            f"{self._base_url}/source/stop/{session_id}",
            timeout=timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def session_stats(
        self, session_id: str, timeout_sec: float = 5.0
    ) -> dict[str, object]:
        """Get statistics for an active session."""
        response = httpx.get(
            f"{self._base_url}/source/session/{session_id}",
            timeout=timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    @property
    def base_url(self) -> str:
        return self._base_url
