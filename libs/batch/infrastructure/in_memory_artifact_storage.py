from __future__ import annotations

from libs.core.application.contracts import ArtifactBlob


class InMemoryArtifactStorage:
    """In-memory artifact storage used by batch pilot engine."""

    def __init__(self) -> None:
        self._reports: dict[str, dict[str, object]] = {}

    def store_frame(self, mission_id: str, frame_id: int, source_uri: str) -> str:
        _ = (mission_id, frame_id)
        return source_uri

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        _ = image_uri
        artifact: ArtifactBlob | None = None
        return artifact

    def save_mission_report(self, mission_id: str, report: dict[str, object]) -> str:
        self._reports[mission_id] = dict(report)
        return f"memory://missions/{mission_id}/report.json"

    def load_mission_report(self, mission_id: str) -> dict[str, object] | None:
        payload = self._reports.get(mission_id)
        return dict(payload) if payload is not None else None
