from __future__ import annotations

import json
import os
from urllib import request


class HttpFramePublisher:
    """HTTP adapter that publishes frame events to mission API."""

    def publish(
        self, mission_id: str, api_base: str, payload: dict[str, object]
    ) -> None:
        url = self.endpoint(mission_id=mission_id, api_base=api_base)
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout_sec = float(os.getenv("DETECTION_HTTP_TIMEOUT_SEC", "1.0"))
        with request.urlopen(req, timeout=timeout_sec):
            return

    def endpoint(self, mission_id: str, api_base: str) -> str:
        return f"{api_base}/v1/missions/{mission_id}/frames"
