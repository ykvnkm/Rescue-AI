from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path

# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=import-outside-toplevel,too-few-public-methods,missing-class-docstring


class LocalArtifactStore:
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir

    def write_report(self, run_key: str, payload: dict[str, object]) -> str:
        path = self._resolve_path(run_key, "report.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(path)

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str:
        path = self._resolve_path(run_key, "debug.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = sorted({key for row in rows for key in row.keys()}) if rows else []
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=headers)
            if headers:
                writer.writeheader()
                writer.writerows(rows)
        return str(path)

    def _resolve_path(self, run_key: str, filename: str) -> Path:
        safe_key = run_key.replace(":", "__")
        return self._root_dir / safe_key / filename


class S3ArtifactStore:
    def __init__(
        self,
        bucket: str,
        prefix: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for S3 artifact store") from exc

        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
        )

    def write_report(self, run_key: str, payload: dict[str, object]) -> str:
        key = self._key(run_key, "report.json")
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
        return f"s3://{self._bucket}/{key}"

    def write_debug_rows(self, run_key: str, rows: list[dict[str, object]]) -> str:
        key = self._key(run_key, "debug.csv")
        headers = sorted({item for row in rows for item in row.keys()}) if rows else []
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=headers)
        if headers:
            writer.writeheader()
            writer.writerows(rows)
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=buffer.getvalue().encode("utf-8")
        )
        return f"s3://{self._bucket}/{key}"

    def _key(self, run_key: str, filename: str) -> str:
        safe_key = run_key.replace(":", "__")
        if not self._prefix:
            return f"{safe_key}/{filename}"
        return f"{self._prefix}/{safe_key}/{filename}"
