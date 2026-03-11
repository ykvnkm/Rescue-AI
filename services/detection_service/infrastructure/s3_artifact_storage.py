from __future__ import annotations

import os
from pathlib import Path

import boto3


class S3ArtifactStorage:
    def __init__(
        self,
        endpoint_url: str,
        region_name: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
    ) -> None:
        self.bucket_name = bucket_name
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    @classmethod
    def from_env(cls) -> "S3ArtifactStorage":
        endpoint_url = os.environ["S3_ENDPOINT"]
        region_name = os.environ["S3_REGION"]
        access_key_id = os.environ["S3_ACCESS_KEY_ID"]
        secret_access_key = os.environ["S3_SECRET_ACCESS_KEY"]
        bucket_name = os.environ["S3_BUCKET_MODELS"]

        return cls(
            endpoint_url=endpoint_url,
            region_name=region_name,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket_name=bucket_name,
        )

    def download_model_if_needed(self, object_key: str, local_path: str) -> str:
        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists() and path.stat().st_size > 0:
            return str(path)

        self.client.download_file(self.bucket_name, object_key, str(path))
        return str(path)