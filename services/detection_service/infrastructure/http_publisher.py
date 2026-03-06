from __future__ import annotations

import json
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
        with request.urlopen(req, timeout=15):
            return

    def endpoint(self, mission_id: str, api_base: str) -> str:
        return f"{api_base}/v1/missions/{mission_id}/frames"
