"""Tests for offline S3 artifact outbox wrapping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.ports import ArtifactStorage as ArtifactStoragePort
from rescue_ai.domain.ports import OutboxRecord, SyncOutbox
from rescue_ai.domain.value_objects import ArtifactBlob
from rescue_ai.infrastructure.sync.outbox_artifact_storage import (
    OutboxArtifactStorage,
    _parse_s3_uri,
)


@dataclass
class _Outbox:
    records: list[OutboxRecord] = field(default_factory=list)

    def enqueue(self, record: OutboxRecord, *, conn: object | None = None) -> None:
        assert conn is None
        self.records.append(record)


@dataclass
class _InnerStorage:
    frame_uri: str = "s3://local/local-prefix/m-1/frames/000001.jpg"
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)

    def store_frame(
        self,
        mission_id: str,
        frame_id: int,
        source_uri: str,
        ds: str,
        *,
        frame_bgr: object | None = None,
    ) -> str:
        assert (mission_id, frame_id, source_uri, ds, frame_bgr) == (
            "m-1",
            1,
            "file:///frame.jpg",
            "2026-06-04",
            None,
        )
        return self.frame_uri

    def save_mission_report(
        self, mission_id: str, ds: str, report: Mapping[str, object]
    ) -> str:
        self.reports["report"] = dict(report)
        return f"s3://local/local-prefix/{ds}/{mission_id}/report.json"

    def save_mission_annotations(
        self, mission_id: str, ds: str, payload: Mapping[str, object]
    ) -> str:
        self.reports["labels"] = dict(payload)
        return f"s3://local/local-prefix/{ds}/{mission_id}/labels.json"

    def save_trajectory_csv(
        self,
        mission_id: str,
        ds: str,
        points: Sequence[TrajectoryPoint],
        *,
        origin: tuple[float, float] | None = None,
    ) -> str:
        self.reports["trajectory"] = {"points": points, "origin": origin}
        return f"s3://local/local-prefix/{ds}/{mission_id}/trajectory.csv"

    def save_trajectory_plot(self, mission_id: str, ds: str, png_bytes: bytes) -> str:
        self.reports["plot"] = {"bytes": png_bytes}
        return f"s3://local/local-prefix/{ds}/{mission_id}/plots/trajectory.png"

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        return ArtifactBlob(b"jpg", "image/jpeg", image_uri.rsplit("/", 1)[-1])

    def load_mission_report(self, mission_id: str, ds: str) -> dict[str, object]:
        return {"mission_id": mission_id, "ds": ds}

    def load_trajectory_plot(self, mission_id: str, ds: str) -> ArtifactBlob:
        assert mission_id and ds
        return ArtifactBlob(b"png", "image/png", "trajectory.png")

    def load_trajectory_csv(self, mission_id: str, ds: str) -> ArtifactBlob:
        assert mission_id and ds
        return ArtifactBlob(b"csv", "text/csv", "trajectory.csv")


def _storage(inner: _InnerStorage, outbox: _Outbox) -> OutboxArtifactStorage:
    return OutboxArtifactStorage(
        inner=cast(ArtifactStoragePort, inner),
        outbox=cast(SyncOutbox, outbox),
        local_bucket="local",
        remote_bucket="remote",
        local_prefix="local-prefix",
        remote_prefix="remote-prefix",
    )


def test_write_methods_enqueue_s3_to_s3_copy_records() -> None:
    inner = _InnerStorage()
    outbox = _Outbox()
    storage = _storage(inner, outbox)

    assert storage.store_frame("m-1", 1, "file:///frame.jpg", "2026-06-04") == (
        "s3://local/local-prefix/m-1/frames/000001.jpg"
    )
    storage.save_mission_report("m-1", "2026-06-04", {"ok": True})
    storage.save_mission_annotations("m-1", "2026-06-04", {"labels": []})
    storage.save_trajectory_csv("m-1", "2026-06-04", [], origin=(55.0, 37.0))
    storage.save_trajectory_plot("m-1", "2026-06-04", b"png")

    assert [record.entity_type for record in outbox.records] == [
        "frame",
        "mission_report",
        "mission_labels",
        "trajectory_csv",
        "trajectory_plot",
    ]
    first = outbox.records[0]
    assert first.source_s3_bucket == "local"
    assert first.source_s3_key == "local-prefix/m-1/frames/000001.jpg"
    assert first.s3_bucket == "remote"
    assert first.s3_key == "remote-prefix/m-1/frames/000001.jpg"
    assert first.idempotency_key == "s3:remote:remote-prefix/m-1/frames/000001.jpg"


def test_non_s3_write_and_reads_do_not_enqueue() -> None:
    inner = _InnerStorage(frame_uri="file:///frame.jpg")
    outbox = _Outbox()
    storage = _storage(inner, outbox)

    assert storage.store_frame("m-1", 1, "file:///frame.jpg", "2026-06-04") == (
        "file:///frame.jpg"
    )
    assert storage.load_frame("s3://x/y") is not None
    assert storage.load_mission_report("m-1", "2026-06-04") == {
        "mission_id": "m-1",
        "ds": "2026-06-04",
    }
    plot = storage.load_trajectory_plot("m-1", "2026-06-04")
    csv = storage.load_trajectory_csv("m-1", "2026-06-04")
    assert plot is not None
    assert csv is not None
    assert plot.filename == "trajectory.png"
    assert csv.media_type == "text/csv"
    assert not outbox.records


def test_prefix_remap_edge_cases_and_s3_uri_parsing() -> None:
    outbox = _Outbox()
    storage = OutboxArtifactStorage(
        inner=cast(ArtifactStoragePort, _InnerStorage()),
        outbox=cast(SyncOutbox, outbox),
        local_bucket="local",
        remote_bucket="remote",
        local_prefix="",
        remote_prefix="remote-prefix",
    )
    assert storage._remap_prefix("x/y.jpg") == "remote-prefix/x/y.jpg"

    storage = OutboxArtifactStorage(
        inner=cast(ArtifactStoragePort, _InnerStorage()),
        outbox=cast(SyncOutbox, outbox),
        local_bucket="local",
        remote_bucket="remote",
        local_prefix="local-prefix",
        remote_prefix="",
    )
    assert storage._remap_prefix("local-prefix/x/y.jpg") == "x/y.jpg"
    assert storage._remap_prefix("other/x/y.jpg") == "other/x/y.jpg"
    assert _parse_s3_uri("s3://bucket/key") == ("bucket", "key")
    assert _parse_s3_uri("file:///tmp/x") is None
    assert _parse_s3_uri("s3://bucket") is None
