"""HTTP frame publisher for the online streaming API."""

from __future__ import annotations

import json
from urllib import request

from rescue_ai.config import get_settings


class HttpFramePublisher:
    """HTTP adapter that publishes frame events to mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: dict[str, object]
    ) -> None:
        """Post a frame event payload to the mission API endpoint."""
        url = self.endpoint(mission_id=mission_id, api_base=api_base)
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout_sec = get_settings().detection.http_timeout_sec
        with request.urlopen(req, timeout=timeout_sec):
            return

    def endpoint(self, mission_id: str, api_base: str) -> str:
        """Build the frames endpoint URL for a given mission."""
        return f"{api_base}/v1/missions/{mission_id}/frames"
