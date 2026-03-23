from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from config import config


@dataclass(frozen=True)
class S3Settings:
    """Connection and namespace settings for S3 stage artifacts."""

    bucket: str
    prefix: str
    endpoint_url: str | None
    region_name: str
    access_key: str | None
    secret_key: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S3-based ML pipeline stage runner")
    parser.add_argument("--stage", required=True, choices=["data", "train", "validate"])
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--ds", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--code-version", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-accuracy", type=float, default=0.75)
    return parser.parse_args()


def build_s3_settings() -> S3Settings:
    bucket = _env_value("BATCH_S3_BUCKET", "ARTIFACTS_S3_BUCKET")
    if not bucket:
        raise ValueError("BATCH_S3_BUCKET/ARTIFACTS_S3_BUCKET is required")

    return S3Settings(
        bucket=bucket,
        prefix=_env_value("BATCH_S3_PREFIX", "ARTIFACTS_S3_PREFIX", default="batch"),
        endpoint_url=_env_value(
            "BATCH_S3_ENDPOINT",
            "ARTIFACTS_S3_ENDPOINT",
            default="",
        )
        or None,
        region_name=_env_value(
            "BATCH_S3_REGION", "ARTIFACTS_S3_REGION", default="us-east-1"
        ),
        access_key=_env_value(
            "BATCH_S3_ACCESS_KEY",
            "ARTIFACTS_S3_ACCESS_KEY_ID",
            default="",
        )
        or None,
        secret_key=_env_value(
            "BATCH_S3_SECRET_KEY",
            "ARTIFACTS_S3_SECRET_ACCESS_KEY",
            default="",
        )
        or None,
    )


def main() -> None:
    args = parse_args()
    s3 = S3IO(build_s3_settings())
    paths = S3Paths(
        prefix=s3.settings.prefix,
        mission_id=args.mission_id,
        ds=args.ds,
        model_version=args.model_version,
        code_version=args.code_version,
    )

    if args.stage == "data":
        run_data_stage(s3=s3, paths=paths, force=args.force)
    elif args.stage == "train":
        run_train_stage(s3=s3, paths=paths, force=args.force)
    else:
        run_validate_stage(
            s3=s3,
            paths=paths,
            force=args.force,
            min_accuracy=args.min_accuracy,
        )


class S3IO:
    """Minimal S3 JSON IO adapter used by pipeline stage handlers."""

    def __init__(self, settings: S3Settings) -> None:
        self.settings = settings
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region_name,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.settings.bucket, Key=key)
            return True
        except ClientError as error:
            status_code = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if status_code == 404:
                return False
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def read_json(self, key: str) -> dict[str, object]:
        response = self._client.get_object(Bucket=self.settings.bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)

    def write_json(self, key: str, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(Bucket=self.settings.bucket, Key=key, Body=body)

    def uri(self, key: str) -> str:
        return f"s3://{self.settings.bucket}/{key}"


class StageStorage(Protocol):
    """Storage interface for stage handlers (real S3 or test doubles)."""

    def exists(self, key: str) -> bool: ...

    def read_json(self, key: str) -> dict[str, object]: ...

    def write_json(self, key: str, payload: dict[str, object]) -> None: ...

    def uri(self, key: str) -> str: ...


@dataclass(frozen=True)
class S3Paths:
    """Deterministic key builder for dataset/model/validation artifacts."""

    prefix: str
    mission_id: str
    ds: str
    model_version: str
    code_version: str

    @property
    def base(self) -> str:
        prefix = self.prefix.strip("/")
        root = f"ml_pipeline/mission={self.mission_id}/ds={self.ds}"
        return f"{prefix}/{root}" if prefix else root

    @property
    def data_key(self) -> str:
        return f"{self.base}/dataset.json"

    @property
    def model_key(self) -> str:
        return (
            f"{self.base}/model_{_slug(self.model_version)}_"
            f"{_slug(self.code_version)}.json"
        )

    @property
    def validation_key(self) -> str:
        return (
            f"{self.base}/validation_{_slug(self.model_version)}_"
            f"{_slug(self.code_version)}.json"
        )


def run_data_stage(s3: StageStorage, paths: S3Paths, force: bool) -> None:
    if s3.exists(paths.data_key) and not force:
        _print_result("data", "idempotent_skip", s3.uri(paths.data_key))
        return

    seed = _stable_number(f"data:{paths.mission_id}:{paths.ds}")
    samples = 100 + (seed % 50)
    positives = max(1, samples // 5 + seed % 7)
    payload = {
        "stage": "data",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "rows_total": samples,
        "rows_positive": positives,
        "feature_hash": hashlib.sha256(
            f"{paths.mission_id}:{paths.ds}".encode("utf-8")
        ).hexdigest()[:16],
    }
    s3.write_json(paths.data_key, payload)
    _print_result("data", "completed", s3.uri(paths.data_key))


def run_train_stage(s3: StageStorage, paths: S3Paths, force: bool) -> None:
    if not s3.exists(paths.data_key):
        raise RuntimeError(f"dataset is missing: {s3.uri(paths.data_key)}")
    if s3.exists(paths.model_key) and not force:
        _print_result("train", "idempotent_skip", s3.uri(paths.model_key))
        return

    dataset = s3.read_json(paths.data_key)
    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    rows_positive = _as_int(dataset.get("rows_positive"), field_name="rows_positive")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")

    class_ratio = rows_positive / rows_total
    payload = {
        "stage": "train",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "model_version": paths.model_version,
        "code_version": paths.code_version,
        "dataset_uri": s3.uri(paths.data_key),
        "class_ratio": round(class_ratio, 6),
        "checkpoint_hash": hashlib.sha256(
            f"{paths.model_version}:{paths.code_version}:{dataset}".encode("utf-8")
        ).hexdigest()[:16],
    }
    s3.write_json(paths.model_key, payload)
    _print_result("train", "completed", s3.uri(paths.model_key))


def run_validate_stage(
    s3: StageStorage,
    paths: S3Paths,
    force: bool,
    min_accuracy: float,
) -> None:
    if not s3.exists(paths.data_key):
        raise RuntimeError(f"dataset is missing: {s3.uri(paths.data_key)}")
    if not s3.exists(paths.model_key):
        raise RuntimeError(f"model artifact is missing: {s3.uri(paths.model_key)}")
    if s3.exists(paths.validation_key) and not force:
        _print_result("validate", "idempotent_skip", s3.uri(paths.validation_key))
        return

    dataset = s3.read_json(paths.data_key)
    model = s3.read_json(paths.model_key)
    rows_total = _as_int(dataset.get("rows_total"), field_name="rows_total")
    class_ratio = _as_float(model.get("class_ratio"), field_name="class_ratio")
    if rows_total <= 0:
        raise RuntimeError("dataset has zero rows")

    stability = (_stable_number(f"val:{paths.ds}:{paths.model_version}") % 7) / 100
    accuracy = round(min(0.99, 0.74 + class_ratio * 0.2 + stability), 4)
    payload = {
        "stage": "validate",
        "created_at": _now_iso(),
        "mission_id": paths.mission_id,
        "ds": paths.ds,
        "dataset_uri": s3.uri(paths.data_key),
        "model_uri": s3.uri(paths.model_key),
        "accuracy": accuracy,
        "min_accuracy": min_accuracy,
        "passed": accuracy >= min_accuracy,
    }
    s3.write_json(paths.validation_key, payload)
    if accuracy < min_accuracy:
        raise RuntimeError(
            f"validation failed: accuracy={accuracy} < threshold={min_accuracy}"
        )
    _print_result("validate", "completed", s3.uri(paths.validation_key))


def _stable_number(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower()).strip(
        "_"
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _env_value(*names: str, default: str | None = None) -> str:
    return config.get_non_empty(*names, default=default)


def _as_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise RuntimeError(f"{field_name} must be an integer") from error
    raise RuntimeError(f"{field_name} must be an integer")


def _as_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be numeric")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as error:
            raise RuntimeError(f"{field_name} must be numeric") from error
    raise RuntimeError(f"{field_name} must be numeric")


def _print_result(stage: str, status: str, output_uri: str) -> None:
    print(
        json.dumps(
            {
                "stage": stage,
                "status": status,
                "output_uri": output_uri,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
