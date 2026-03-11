from __future__ import annotations

import json
from urllib import request
from urllib.error import HTTPError


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
        try:
            with request.urlopen(req, timeout=15):
                return
        except HTTPError as error:
            detail = _extract_http_error_detail(error)
            raise RuntimeError(
                f"Frame publish failed: HTTP {error.code}: {detail}"
            ) from error

    def endpoint(self, mission_id: str, api_base: str) -> str:
        return f"{api_base}/v1/missions/{mission_id}/frames"


def _extract_http_error_detail(error: HTTPError) -> str:
    try:
        raw_body = error.read().decode("utf-8", errors="replace").strip()
    except (OSError, AttributeError, UnicodeDecodeError):
        return error.reason if isinstance(error.reason, str) else str(error)

    if not raw_body:
        return error.reason if isinstance(error.reason, str) else str(error)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body

    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    return raw_body
