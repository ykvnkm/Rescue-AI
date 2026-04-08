"""Tests for the unified pipeline stage functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from rescue_ai.application.batch_dtos import FrameRecord, MissionInput
from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    run_data_stage,
    run_evaluate_stage,
    run_warmup_stage,
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
        ds="2026-03-01",
        model_version="yolov8n",
        code_version="v1",
    )


# ── PipelinePaths ───────────────────────────────────────────────


class TestPipelinePaths:
    def test_base_with_prefix(self, paths: PipelinePaths) -> None:
        assert paths.base == "test/ml_pipeline/mission=mission-1/ds=2026-03-01"

    def test_base_without_prefix(self) -> None:
        p = PipelinePaths(
            prefix="", mission_id="m", ds="d", model_version="mv", code_version="cv"
        )
        assert p.base == "ml_pipeline/mission=m/ds=d"

    def test_all_keys_unique(self, paths: PipelinePaths) -> None:
        keys = {
            paths.data_key,
            paths.model_key,
            paths.evaluation_key,
        }
        assert len(keys) == 3


# ── Data stage ──────────────────────────────────────────────────


class TestDataStage:
    @staticmethod
    def _mission_loader_variant(include_extra: bool = False) -> MissionInput:
        frames = [
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
        if include_extra:
            frames.append(
                FrameRecord(
                    frame_id=4,
                    ts_sec=0.3,
                    frame_path=Path("/tmp/f4.jpg"),
                    image_uri="/tmp/f4.jpg",
                    gt_person_present=True,
                    is_corrupted=False,
                )
            )
        return MissionInput(
            source_uri="local:///mission-1/2026-03-01",
            frames=frames,
            gt_available=True,
        )

    @staticmethod
    def _mission_loader() -> MissionInput:
        return TestDataStage._mission_loader_variant()

    def test_creates_dataset(self, store, paths) -> None:
        result = run_data_stage(store, paths, mission_loader=self._mission_loader)
        assert result["status"] == "completed"
        assert store.exists(paths.data_key)
        payload = store.read_json(paths.data_key)
        assert payload["stage"] == "data"
        assert payload["rows_total"] > 0
        assert payload["evaluation_count"] == payload["rows_total"]
        assert payload["rows_corrupted"] == 1
        assert len(payload["evaluation_manifest"]) > 0

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=self._mission_loader)
        result = run_data_stage(store, paths, mission_loader=self._mission_loader)
        assert result["status"] == "idempotent_skip"

    def test_rerun_rebuilds_when_source_changes(self, store, paths) -> None:
        run_data_stage(
            store,
            paths,
            mission_loader=lambda: self._mission_loader_variant(include_extra=False),
        )
        result = run_data_stage(
            store,
            paths,
            mission_loader=lambda: self._mission_loader_variant(include_extra=True),
        )
        assert result["status"] == "completed"
        payload = store.read_json(paths.data_key)
        assert payload["rows_total"] == 3
        assert payload["evaluation_count"] == 3

    def test_force_overwrites(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=self._mission_loader)
        result = run_data_stage(
            store,
            paths,
            force=True,
            mission_loader=self._mission_loader,
        )
        assert result["status"] == "completed"

    def test_requires_mission_loader(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="mission_loader is required"):
            run_data_stage(store, paths)


# ── Warmup stage ────────────────────────────────────────────────


class TestWarmupStage:
    @staticmethod
    def _model_probe() -> dict[str, object]:
        return {
            "runtime": "fake",
            "model_url": "https://example.test/model.pt",
            "model_ready": True,
        }

    def test_creates_model_card(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        result = run_warmup_stage(store, paths, model_probe=self._model_probe)
        assert result["status"] == "completed"
        assert store.exists(paths.model_key)
        payload = store.read_json(paths.model_key)
        assert payload["stage"] == "warmup"
        assert "checkpoint_hash" in payload
        assert payload["model_runtime"] == "fake"

    def test_requires_dataset(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_warmup_stage(store, paths, model_probe=self._model_probe)

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        run_warmup_stage(store, paths, model_probe=self._model_probe)
        result = run_warmup_stage(store, paths, model_probe=self._model_probe)
        assert result["status"] == "idempotent_skip"

    def test_rerun_rebuilds_when_dataset_changes(self, store, paths) -> None:
        run_data_stage(
            store,
            paths,
            mission_loader=lambda: TestDataStage._mission_loader_variant(
                include_extra=False
            ),
        )
        run_warmup_stage(store, paths, model_probe=self._model_probe)
        run_data_stage(
            store,
            paths,
            mission_loader=lambda: TestDataStage._mission_loader_variant(
                include_extra=True
            ),
        )
        result = run_warmup_stage(store, paths, model_probe=self._model_probe)
        assert result["status"] == "completed"

    def test_requires_model_probe(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        with pytest.raises(RuntimeError, match="model_probe is required"):
            run_warmup_stage(store, paths)


# ── Evaluate stage ──────────────────────────────────────────────


class TestEvaluateStage:
    @staticmethod
    def _predict_all_correct(image_uri: str) -> bool:
        return image_uri.endswith("f1.jpg")

    def test_smoke_test_passes(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)
        result = run_evaluate_stage(
            store,
            paths,
            detector_predict=self._predict_all_correct,
        )
        assert result["status"] == "completed"
        payload = store.read_json(paths.evaluation_key)
        assert payload["passed"] is True

    def test_smoke_test_fails_when_detector_raises(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)

        def _broken(_image_uri: str) -> bool:
            raise RuntimeError("detector is down")

        with pytest.raises(RuntimeError, match="detector failed"):
            run_evaluate_stage(
                store,
                paths,
                detector_predict=_broken,
            )

    def test_requires_dataset(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_evaluate_stage(store, paths, detector_predict=lambda _: True)

    def test_requires_model(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        with pytest.raises(RuntimeError, match="model artifact is missing"):
            run_evaluate_stage(store, paths, detector_predict=lambda _: True)

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)
        run_evaluate_stage(
            store,
            paths,
            detector_predict=self._predict_all_correct,
        )
        result = run_evaluate_stage(
            store,
            paths,
            detector_predict=self._predict_all_correct,
        )
        assert result["status"] == "idempotent_skip"

    def test_rerun_rebuilds_when_dataset_changes(self, store, paths) -> None:
        run_data_stage(
            store,
            paths,
            mission_loader=lambda: TestDataStage._mission_loader_variant(
                include_extra=False
            ),
        )
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)
        run_evaluate_stage(store, paths, detector_predict=self._predict_all_correct)
        run_data_stage(
            store,
            paths,
            mission_loader=lambda: TestDataStage._mission_loader_variant(
                include_extra=True
            ),
        )
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)
        result = run_evaluate_stage(
            store,
            paths,
            detector_predict=self._predict_all_correct,
        )
        assert result["status"] == "completed"

    def test_requires_detector_predict(self, store, paths) -> None:
        run_data_stage(store, paths, mission_loader=TestDataStage._mission_loader)
        run_warmup_stage(store, paths, model_probe=TestWarmupStage._model_probe)
        with pytest.raises(RuntimeError, match="detector_predict is required"):
            run_evaluate_stage(store, paths)
