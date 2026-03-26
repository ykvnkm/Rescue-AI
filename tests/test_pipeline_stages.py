"""Tests for the unified pipeline stage functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from rescue_ai.application.pipeline_stages import (
    PipelinePaths,
    run_data_stage,
    run_inference_stage,
    run_train_stage,
    run_validate_stage,
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
            paths.validation_key,
            paths.inference_key,
        }
        assert len(keys) == 4


# ── Data stage ──────────────────────────────────────────────────


class TestDataStage:
    def test_creates_dataset(self, store, paths) -> None:
        result = run_data_stage(store, paths)
        assert result["status"] == "completed"
        assert store.exists(paths.data_key)
        payload = store.read_json(paths.data_key)
        assert payload["stage"] == "data"
        assert payload["rows_total"] > 0
        assert payload["train_count"] + payload["val_count"] == payload["rows_total"]

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths)
        result = run_data_stage(store, paths)
        assert result["status"] == "idempotent_skip"

    def test_force_overwrites(self, store, paths) -> None:
        run_data_stage(store, paths)
        result = run_data_stage(store, paths, force=True)
        assert result["status"] == "completed"


# ── Train stage ─────────────────────────────────────────────────


class TestTrainStage:
    def test_creates_model_card(self, store, paths) -> None:
        run_data_stage(store, paths)
        result = run_train_stage(store, paths)
        assert result["status"] == "completed"
        assert store.exists(paths.model_key)
        payload = store.read_json(paths.model_key)
        assert payload["stage"] == "train"
        assert "checkpoint_hash" in payload

    def test_requires_dataset(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_train_stage(store, paths)

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        result = run_train_stage(store, paths)
        assert result["status"] == "idempotent_skip"


# ── Validate stage ──────────────────────────────────────────────


class TestValidateStage:
    def test_passes_quality_gate(self, store, paths) -> None:
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        result = run_validate_stage(store, paths, min_accuracy=0.01)
        assert result["status"] == "completed"
        payload = store.read_json(paths.validation_key)
        assert payload["passed"] is True

    def test_fails_quality_gate(self, store, paths) -> None:
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        with pytest.raises(RuntimeError, match="validation failed"):
            run_validate_stage(store, paths, min_accuracy=1.0)

    def test_requires_dataset(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="dataset is missing"):
            run_validate_stage(store, paths)

    def test_requires_model(self, store, paths) -> None:
        run_data_stage(store, paths)
        with pytest.raises(RuntimeError, match="model artifact is missing"):
            run_validate_stage(store, paths)

    def test_idempotent_skip(self, store, paths) -> None:
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        run_validate_stage(store, paths, min_accuracy=0.01)
        result = run_validate_stage(store, paths, min_accuracy=0.01)
        assert result["status"] == "idempotent_skip"


# ── Inference stage ─────────────────────────────────────────────


class TestInferenceStage:
    def _seed_through_validation(self, store, paths) -> None:
        """Run data → train → validate so inference can proceed."""
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        run_validate_stage(store, paths, min_accuracy=0.01)

    def test_requires_validation(self, store, paths) -> None:
        with pytest.raises(RuntimeError, match="validation artifact is missing"):
            run_inference_stage(store, paths)

    def test_blocks_on_failed_validation(self, store, paths) -> None:
        run_data_stage(store, paths)
        run_train_stage(store, paths)
        # Write a "failed" validation artifact manually
        store.write_json(
            paths.validation_key,
            {"passed": False, "accuracy": 0.5, "stage": "validate"},
        )
        with pytest.raises(RuntimeError, match="validation did not pass"):
            run_inference_stage(store, paths, runner_factory=lambda: None)

    def test_requires_runner_factory(self, store, paths) -> None:
        self._seed_through_validation(store, paths)
        with pytest.raises(RuntimeError, match="runner_factory is required"):
            run_inference_stage(store, paths)

    def test_runs_with_fake_runner(self, store, paths) -> None:
        self._seed_through_validation(store, paths)

        class FakeResult:
            run_key = "rk"
            status = "completed"
            report_uri = "report://uri"
            debug_uri = "debug://uri"

        class FakeRunner:
            def run(self, request):
                return FakeResult()

        class FakeRequest:
            pass

        def factory():
            return FakeRunner(), FakeRequest()

        result = run_inference_stage(store, paths, runner_factory=factory)
        assert result["status"] == "completed"
        payload = store.read_json(paths.inference_key)
        assert payload["stage"] == "inference"
        assert payload["run_key"] == "rk"

    def test_idempotent_skip(self, store, paths) -> None:
        self._seed_through_validation(store, paths)

        class FakeResult:
            run_key = "rk"
            status = "completed"
            report_uri = "r"
            debug_uri = "d"

        class FakeRunner:
            def run(self, request):
                return FakeResult()

        def factory():
            return FakeRunner(), object()

        run_inference_stage(store, paths, runner_factory=factory)
        result = run_inference_stage(store, paths, runner_factory=factory)
        assert result["status"] == "idempotent_skip"
