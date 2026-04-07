"""JSON and Postgres status store implementations."""

from __future__ import annotations

import json
from pathlib import Path

from rescue_ai.application.batch_dtos import RunStatusRecord


class JsonStatusStore:
    """Status store backed by a local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, run_key: str) -> RunStatusRecord | None:
        data = self._read_all()
        payload = data.get(run_key)
        if payload is None:
            return None
        return RunStatusRecord(
            run_key=run_key,
            status=str(payload.get("status", "pending")),
            reason=_as_optional_str(payload.get("reason")),
            report_uri=_as_optional_str(payload.get("report_uri")),
            debug_uri=_as_optional_str(payload.get("debug_uri")),
        )

    def upsert(self, record: RunStatusRecord) -> None:
        data = self._read_all()
        data[record.run_key] = {
            "status": record.status,
            "reason": record.reason,
            "report_uri": record.report_uri,
            "debug_uri": record.debug_uri,
        }
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_all(self) -> dict[str, dict[str, str | None]]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value for key, value in payload.items() if isinstance(value, dict)
        }


def _as_optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None
