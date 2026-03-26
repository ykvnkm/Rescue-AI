"""Stage artifact storage adapters for the ML pipeline."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None
    ClientError = Exception


class LocalStageStore:
    """Stores pipeline stage artifacts as local JSON files."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def read_json(self, key: str) -> dict[str, object]:
        return json.loads(self._path(key).read_text(encoding="utf-8"))

    def write_json(self, key: str, payload: dict[str, object]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def uri(self, key: str) -> str:
        return str(self._path(key))

    def _path(self, key: str) -> Path:
        return self._root / key.replace(":", "__")


class S3StageStore:
    """Stores pipeline stage artifacts as JSON objects in S3."""

    def __init__(self, settings: object) -> None:
        """Accept an S3Settings-like object with credentials."""
        if boto3 is None:
            raise RuntimeError("boto3 is required for S3 stage storage")
        self._bucket = str(getattr(settings, "bucket", "") or "")
        self._client = boto3.client(
            "s3",
            endpoint_url=getattr(settings, "endpoint", None),
            region_name=getattr(settings, "region", "us-east-1"),
            aws_access_key_id=getattr(settings, "access_key_id", None),
            aws_secret_access_key=getattr(settings, "secret_access_key", None),
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as error:
            code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == 404:
                return False
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def read_json(self, key: str) -> dict[str, object]:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)

    def write_json(self, key: str, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body)

    def uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"
