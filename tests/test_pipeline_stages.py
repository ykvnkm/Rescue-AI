"""Tests for the canonical batch pipeline stage functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from rescue_ai.application.batch_dtos import FrameRecord, MissionInput
from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    run_evaluate_model_stage,
    run_prepare_dataset_stage,
    run_publish_metrics_stage,
)
from rescue_ai.infrastructure.stage_store import LocalStageStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalStageStore:
    return LocalStageStore(root=tmp_path / "stages")


@pytest.fixture()
def paths() -> PipelinePaths:
    return PipelinePaths(
        prefix="test",
        mission_id="mission-1",
        ds="2026-04-09",
    )


def _frames(*, with_extra: bool = False) -> list[FrameRecord]:
    base = [
        FrameRecord(
            frame_id=1,
            ts_sec=0.0,
            frame_path=Path("/tmp/f1.jpg"),
            image_uri="/tmp/f1.jpg",
            gt_person_present=True,
            is_corrupted=False,
        ),
        FrameRecord(
            frame_id=2,
            ts_sec=0.1,
            frame_path=Path("/tmp/f2.jpg"),
            image_uri="/tmp/f2.jpg",
            gt_person_present=False,
            is_corrupted=False,
        ),
        FrameRecord(
            frame_id=3,
            ts_sec=0.2,
            frame_path=Path("/tmp/f3.jpg"),
            image_uri="/tmp/f3.jpg",
            gt_person_present=True,
            is_corrupted=True,
        ),
    ]
    if with_extra:
        base.append(
            FrameRecord(
                frame_id=4,
                ts_sec=0.3,
                frame_path=Path("/tmp/f4.jpg"),
                image_uri="/tmp/f4.jpg",
                gt_person_present=True,
                is_corrupted=False,
            )
        )
    return base


def _mission(with_extra: bool = False) -> MissionInput:
    return MissionInput(
        source_uri="s3://bucket/missions/ds=2026-04-09/mission-1",
        frames=_frames(with_extra=with_extra),
        gt_available=True,
    )


def _mission_loader() -> MissionInput:
    return _mission()


def _mission_loader_extra() -> MissionInput:
    return _mission(with_extra=True)


def _predict_true(_image_uri: str) -> bool:
    return True


def _predict_false(_image_uri: str) -> bool:
    return False


# ── PipelinePaths ───────────────────────────────────────────────


class TestPipelinePaths:
    def test_base_with_prefix(self, paths: PipelinePaths) -> None:
        assert paths.base == "test/ml_pipeline/ds=2026-04-09/mission=mission-1"

    def test_base_without_prefix(self) -> None:
        p = PipelinePaths(
            prefix="",
            mission_id="m",
            ds="2026-04-09",
        )
        assert p.base == "ml_pipeline/ds=2026-04-09/mission=m"

    def test_dataset_and_evaluation_keys_distinct(self, paths: PipelinePaths) -> None:
        assert paths.dataset_key.endswith("/dataset.json")
        assert paths.evaluation_key.endswith("/evaluation.json")
        assert paths.dataset_key != paths.evaluation_key


# ── prepare_dataset ─────────────────────────────────────────────


class TestPrepareDatasetStage:
    def test_writes_manifest(self, store, paths) -> None:
        result = run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        assert result["status"] == "completed"
        assert store.exists(paths.dataset_key)
        payload = store.read_json(paths.dataset_key)
        assert payload["stage"] == "prepare_dataset"
        assert payload["rows_total"] == 2  # one corrupted frame dropped
        assert payload["rows_corrupted"] == 1
        assert len(payload["evaluation_manifest"]) == 2

    def test_rerun_overwrites_with_new_frames(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader_extra)
        payload = store.read_json(paths.dataset_key)
        assert payload["rows_total"] == 3
        assert payload["evaluation_count"] == 3

    def test_empty_input_raises(self, store, paths) -> None:
        empty = MissionInput(source_uri="s", frames=[], gt_available=True)
        with pytest.raises(RuntimeError, match="no valid frames"):
            run_prepare_dataset_stage(store, paths, mission_loader=lambda: empty)


# ── evaluate_model ──────────────────────────────────────────────


def _predict_only_f1(image_uri: str) -> bool:
    return image_uri.endswith("f1.jpg")


class TestEvaluateModelStage:
    def test_writes_evaluation(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        result = run_evaluate_model_stage(
            store, paths, detector_predict=_predict_only_f1
        )
        assert result["status"] == "completed"
        payload = store.read_json(paths.evaluation_key)
        assert payload["gt_available"] is True
        assert payload["tp"] == 1  # f1 is positive and detected
        assert payload["tn"] == 1  # f2 is negative and not detected

    def test_requires_dataset(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_evaluate_model_stage(store, paths, detector_predict=_predict_true)

    def test_detector_failure_propagates(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)

        def _broken(_uri: str) -> bool:
            raise RuntimeError("detector down")

        with pytest.raises(RuntimeError, match="detector failed"):
            run_evaluate_model_stage(store, paths, detector_predict=_broken)

    def test_rerun_overwrites_evaluation(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        run_evaluate_model_stage(store, paths, detector_predict=_predict_only_f1)
        # Rerun with a different predictor → evaluation must be overwritten,
        # not skipped.
        run_evaluate_model_stage(store, paths, detector_predict=_predict_false)
        payload = store.read_json(paths.evaluation_key)
        assert payload["tp"] == 0
        assert payload["fn"] == 1


# ── publish_metrics ─────────────────────────────────────────────


class _FakeMetricsWriter:
    def __init__(self) -> None:
        self.records: list[object] = []

    def upsert(self, record: object) -> None:
        self.records.append(record)


def _record_factory(*, paths, dataset, evaluation):
    return {
        "ds": paths.ds,
        "mission_id": paths.mission_id,
        "rows_total": dataset.get("rows_total"),
        "tp": evaluation.get("tp"),
        "fp": evaluation.get("fp"),
    }


class TestPublishMetricsStage:
    def test_upserts_record(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        run_evaluate_model_stage(store, paths, detector_predict=_predict_only_f1)
        writer = _FakeMetricsWriter()
        result = run_publish_metrics_stage(
            store,
            paths,
            metrics_writer=writer,
            record_factory=_record_factory,
        )
        assert result["status"] == "completed"
        assert len(writer.records) == 1

    def test_requires_dataset(self, store, paths) -> None:
        writer = _FakeMetricsWriter()
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_publish_metrics_stage(
                store,
                paths,
                metrics_writer=writer,
                record_factory=_record_factory,
            )

    def test_requires_evaluation(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        writer = _FakeMetricsWriter()
        with pytest.raises(RuntimeError, match="evaluation is missing"):
            run_publish_metrics_stage(
                store,
                paths,
                metrics_writer=writer,
                record_factory=_record_factory,
            )

    def test_rerun_calls_upsert_again(self, store, paths) -> None:
        run_prepare_dataset_stage(store, paths, mission_loader=_mission_loader)
        run_evaluate_model_stage(store, paths, detector_predict=_predict_only_f1)
        writer = _FakeMetricsWriter()
        run_publish_metrics_stage(
            store,
            paths,
            metrics_writer=writer,
            record_factory=_record_factory,
        )
        run_publish_metrics_stage(
            store,
            paths,
            metrics_writer=writer,
            record_factory=_record_factory,
        )
        assert len(writer.records) == 2  # no skip-by-exists
