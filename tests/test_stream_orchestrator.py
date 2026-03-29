"""Unit tests for stream orchestrator."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from rescue_ai.application.inference_config import InferenceConfig
from rescue_ai.application.stream_orchestrator import StreamConfig, StreamOrchestrator
from rescue_ai.domain.entities import Detection


class _FakeAnnotationIndex:
    def get_gt_boxes(self, frame_path: Path) -> list[tuple[float, float, float, float]]:
        if "0001" in frame_path.name:
            return [(0.0, 0.0, 1.0, 1.0)]
        return []


class _FakeDetector:
    def warmup(self) -> None:
        return None

    def detect(self, image_uri: str) -> list[Detection]:
        _ = image_uri
        return [
            Detection(
                bbox=(1.0, 2.0, 3.0, 4.0),
                score=0.95,
                label="person",
                model_name="fake",
            )
        ]

    def runtime_name(self) -> str:
        return "fake"


class _FailingDetector(_FakeDetector):
    def detect(self, image_uri: str) -> list[Detection]:
        _ = image_uri
        raise RuntimeError("detector error")


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(self, mission_id: str, api_base: str, payload) -> None:
        self.calls.append(
            {"mission_id": mission_id, "api_base": api_base, "payload": payload}
        )

    def endpoint(self, mission_id: str, api_base: str) -> str:
        return f"{api_base}/mission/{mission_id}"


def _build_config(frame_files: list[Path], mission_id: str = "m1") -> StreamConfig:
    return StreamConfig(
        mission_id=mission_id,
        frame_files=frame_files,
        fps=1000.0,
        api_base="http://localhost:8000",
        annotations=_FakeAnnotationIndex(),
        inference=InferenceConfig(
            model_url="s3://bucket/model.pt",
            device="cpu",
            imgsz=640,
            nms_iou=0.5,
            max_det=10,
            confidence_threshold=0.25,
        ),
        min_detections_per_frame=1,
    )


def test_stream_orchestrator_processes_frames_to_completion() -> None:
    publisher = _FakePublisher()
    orchestrator = StreamOrchestrator(
        detector_factory=lambda _: _FakeDetector(),
        frame_publisher=publisher,
    )

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        frame_files = [root / "frame_0001.jpg", root / "frame_0002.jpg"]
        for frame in frame_files:
            frame.write_bytes(b"\xff\xd8\xff\xd9")

        state = orchestrator.start_stream(_build_config(frame_files=frame_files))
        assert state.running is True

        final_state = orchestrator.wait_stream_stopped("m1", timeout_sec=2.0)

    assert final_state is not None
    assert final_state.running is False
    assert final_state.processed_frames == 2
    assert final_state.last_frame_name == "frame_0002.jpg"
    assert final_state.error is None
    assert len(publisher.calls) == 2


def test_stream_orchestrator_stop_stream() -> None:
    publisher = _FakePublisher()
    orchestrator = StreamOrchestrator(
        detector_factory=lambda _: _FakeDetector(),
        frame_publisher=publisher,
    )

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        frame_files = [root / f"frame_{idx:04d}.jpg" for idx in range(1, 6)]
        for frame in frame_files:
            frame.write_bytes(b"\xff\xd8\xff\xd9")

        orchestrator.start_stream(
            _build_config(frame_files=frame_files, mission_id="m2")
        )
        stopped = orchestrator.stop_stream("m2")
        assert stopped is not None
        assert stopped.stop_requested is True

        final_state = orchestrator.wait_stream_stopped("m2", timeout_sec=2.0)

    assert final_state is not None
    assert final_state.running is False


def test_stream_orchestrator_handles_detector_error() -> None:
    publisher = _FakePublisher()
    orchestrator = StreamOrchestrator(
        detector_factory=lambda _: _FailingDetector(),
        frame_publisher=publisher,
    )

    with TemporaryDirectory() as temp_dir:
        frame = Path(temp_dir) / "frame_0001.jpg"
        frame.write_bytes(b"\xff\xd8\xff\xd9")

        orchestrator.start_stream(_build_config(frame_files=[frame], mission_id="m3"))
        final_state = orchestrator.wait_stream_stopped("m3", timeout_sec=2.0)

    assert final_state is not None
    assert final_state.running is False
    assert "detector error" in (final_state.error or "")
    assert not publisher.calls


def test_stream_orchestrator_rejects_duplicate_start_and_missing_stop() -> None:
    publisher = _FakePublisher()
    orchestrator = StreamOrchestrator(
        detector_factory=lambda _: _FakeDetector(),
        frame_publisher=publisher,
    )

    with TemporaryDirectory() as temp_dir:
        frame = Path(temp_dir) / "frame_0001.jpg"
        frame.write_bytes(b"\xff\xd8\xff\xd9")
        config = _build_config(frame_files=[frame], mission_id="m4")

        orchestrator.start_stream(config)
        try:
            orchestrator.start_stream(config)
            assert False, "Expected ValueError on duplicate start"
        except ValueError:
            pass

        assert orchestrator.stop_stream("unknown") is None
