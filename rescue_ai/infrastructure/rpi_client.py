"""HTTP client for Raspberry Pi frame source service."""

from __future__ import annotations

import re
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
    stream_url: str = ""


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
        loop: bool = False,
        target_fps: float = 6.0,
        timeout_sec: float = 15.0,
    ) -> RpiStreamSession:
        """Start a streaming session on the RPi."""
        mission_path = self._resolve_mission_path(mission_id=mission_id)
        response = httpx.post(
            f"{self._base_url}/source/start",
            json={
                "mode": "frames",
                "source": mission_path,
                "mission_id": mission_id,
                "realtime": True,
                "loop": loop,
                "target_fps": target_fps,
            },
            timeout=timeout_sec,
        )
        if response.status_code == 404:
            raise ValueError(f"RPi mission not found: {mission_id}")
        response.raise_for_status()
        data = response.json()
        session_id = data.get("session_id", "")

        # Use rtsp_url from RPi response if provided, else construct manually
        rtsp_url = data.get("rtsp_url", "")
        if not rtsp_url:
            rtsp_host = self._base_url.split("://")[-1].split(":")[0]
            rtsp_suffix = f"/{session_id}" if session_id else ""
            rtsp_url = (
                f"rtsp://{rtsp_host}:{self._rtsp_port}"
                f"/{self._rtsp_path_prefix}{rtsp_suffix}"
            )

        # HTTP stream fallback URL
        stream_path = data.get("stream_url", "")
        stream_url = f"{self._base_url}{stream_path}" if stream_path else ""

        return RpiStreamSession(
            session_id=session_id,
            rtsp_url=rtsp_url,
            stream_url=stream_url,
        )

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

    def load_gt_sequence(
        self,
        mission_id: str,
        timeout_sec: float = 15.0,
    ) -> list[bool] | None:
        """Load per-frame GT person presence sequence from mission COCO annotations."""
        catalog = self.catalog(timeout_sec=timeout_sec)
        mission = next(
            (item for item in catalog.missions if item.mission_id == mission_id),
            None,
        )
        if mission is None or not mission.annotations_json:
            return None

        response = httpx.get(
            f"{self._base_url}/source/raw_file",
            params={"path": mission.annotations_json},
            timeout=timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return _build_gt_sequence_from_coco(payload)

    @property
    def base_url(self) -> str:
        return self._base_url


def _build_gt_sequence_from_coco(
    payload: dict[str, object],
) -> list[bool] | None:
    images_raw = payload.get("images")
    annotations_raw = payload.get("annotations")
    if not isinstance(images_raw, list) or not isinstance(annotations_raw, list):
        return None

    image_rows = [item for item in images_raw if isinstance(item, dict)]
    if not image_rows:
        return None

    person_category_ids = _extract_person_category_ids(payload.get("categories"))
    positive_image_ids = _extract_positive_image_ids(
        annotations_raw,
        person_category_ids,
    )
    numbered_sequence = _build_numbered_sequence(image_rows, positive_image_ids)
    if numbered_sequence is not None:
        return numbered_sequence
    return _build_sorted_sequence(image_rows, positive_image_ids)


def _extract_person_category_ids(category_rows: object) -> set[int]:
    if not isinstance(category_rows, list):
        return set()
    person_category_ids: set[int] = set()
    for item in category_rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        if name != "person":
            continue
        category_id = _to_int(item.get("id"))
        if category_id is not None:
            person_category_ids.add(category_id)
    return person_category_ids


def _extract_positive_image_ids(
    annotations_raw: list[object],
    person_category_ids: set[int],
) -> set[int]:
    positive_image_ids: set[int] = set()
    for item in annotations_raw:
        if not isinstance(item, dict):
            continue
        image_id = _to_int(item.get("image_id"))
        if image_id is None:
            continue
        if person_category_ids:
            category_id = _to_int(item.get("category_id"))
            if category_id not in person_category_ids:
                continue
        positive_image_ids.add(image_id)
    return positive_image_ids


def _build_numbered_sequence(
    image_rows: list[dict[object, object]],
    positive_image_ids: set[int],
) -> list[bool] | None:
    image_id_to_frame_num: dict[int, int] = {}
    for row in image_rows:
        image_id = _to_int(row.get("id"))
        if image_id is None:
            continue
        frame_num = _extract_frame_num(row)
        if frame_num is None:
            return None
        image_id_to_frame_num[image_id] = frame_num

    if not image_id_to_frame_num:
        return None
    min_num = min(image_id_to_frame_num.values())
    max_num = max(image_id_to_frame_num.values())
    if max_num < min_num:
        return None
    size = max_num - min_num + 1
    sequence = [False] * size
    for image_id in positive_image_ids:
        frame_num = image_id_to_frame_num.get(image_id)
        if frame_num is None:
            continue
        idx = frame_num - min_num
        if 0 <= idx < size:
            sequence[idx] = True
    return sequence


def _extract_frame_num(row: dict[object, object]) -> int | None:
    name = str(row.get("file_name", ""))
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    match = re.search(r"(\d+)$", stem)
    if match is None:
        return None
    return int(match.group(1))


def _build_sorted_sequence(
    image_rows: list[dict[object, object]],
    positive_image_ids: set[int],
) -> list[bool]:
    sorted_images = sorted(
        image_rows,
        key=lambda row: (
            str(row.get("file_name", "")),
            _to_int(row.get("id")) or 0,
        ),
    )
    sequence: list[bool] = []
    for row in sorted_images:
        image_id = _to_int(row.get("id"))
        sequence.append(
            image_id in positive_image_ids if image_id is not None else False
        )
    return sequence


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return None
    return None
