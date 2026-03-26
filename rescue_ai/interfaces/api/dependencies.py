"""FastAPI dependency injection providers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypedDict

from rescue_ai.application.annotation_index import build_annotation_index
from rescue_ai.application.frame_source import FrameSourceService
from rescue_ai.application.pilot_service import PilotService
from rescue_ai.application.stream_orchestrator import (
    StreamConfig,
    StreamOrchestrator,
    StreamState,
)
from rescue_ai.config import get_settings
from rescue_ai.domain.entities import AlertRuleConfig
from rescue_ai.domain.ports import (
    AlertRepository,
    FrameEventRepository,
    MissionRepository,
)
from rescue_ai.infrastructure.contract_loader import (
    StreamContract,
    load_alert_rules_and_metadata,
    load_stream_contract,
)
from rescue_ai.infrastructure.http_publisher import HttpFramePublisher
from rescue_ai.infrastructure.memory_repositories import (
    InMemoryAlertRepository,
    InMemoryDatabase,
    InMemoryFrameEventRepository,
    InMemoryMissionRepository,
)
from rescue_ai.infrastructure.s3_artifact_store import build_artifact_storage
from rescue_ai.infrastructure.yolo_detector import YoloDetector


class StreamOptions(TypedDict):
    """External options accepted when creating stream configuration."""

    frames_dir: str
    annotations_path: str | None
    fps: float
    api_base: str


class DetectionStreamController:
    """Owns the stream orchestrator and exposes stream lifecycle methods."""

    def __init__(self) -> None:
        self._frame_source = FrameSourceService()
        self._orchestrator = StreamOrchestrator(
            detector_factory=YoloDetector,
            frame_publisher=HttpFramePublisher(),
            frame_source=self._frame_source,
        )

    def build_config(
        self,
        mission_id: str,
        options: StreamOptions,
        contract: StreamContract | None = None,
        frame_source: FrameSourceService | None = None,
    ) -> StreamConfig:
        """Build a StreamConfig from mission options and contract defaults."""
        resolved_contract = contract or load_stream_contract()
        source = frame_source or self._frame_source

        frames_path = Path(options["frames_dir"])
        if not frames_path.exists() or not frames_path.is_dir():
            raise ValueError(f"frames dir not found: {frames_path}")

        frame_files = source.list_frame_files(frames_path)
        if not frame_files:
            raise ValueError("no frames found")

        annotations = build_annotation_index(
            frames_dir=frames_path,
            explicit_path=options["annotations_path"],
        )

        fps = options["fps"]
        if fps <= 0:
            fps = resolved_contract.dataset_fps

        return StreamConfig(
            mission_id=mission_id,
            frame_files=frame_files,
            fps=fps,
            api_base=options["api_base"],
            annotations=annotations,
            inference=resolved_contract.inference,
            min_detections_per_frame=resolved_contract.min_detections_per_frame,
        )

    def start(self, config: StreamConfig) -> StreamState:
        """Start a detection stream with the given configuration."""
        return self._orchestrator.start_stream(config)

    def stop(self, mission_id: str) -> StreamState | None:
        """Request graceful stop of the stream for a mission."""
        return self._orchestrator.stop_stream(mission_id)

    def wait_stopped(
        self, mission_id: str, timeout_sec: float = 3.0
    ) -> StreamState | None:
        """Block until the stream has stopped or timeout expires."""
        return self._orchestrator.wait_stream_stopped(
            mission_id=mission_id,
            timeout_sec=timeout_sec,
        )

    def get_state(self, mission_id: str) -> StreamState | None:
        """Return current stream state for a mission, or None."""
        return self._orchestrator.get_stream_state(mission_id)


@dataclass
class AppContainer:
    """Process-level dependencies for API runtime."""

    pilot_service: PilotService
    stream_controller: DetectionStreamController
    reset_hook: Callable[[], None]


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    alert_rules, report_metadata = load_alert_rules_and_metadata()
    mission_repository, alert_repository, frame_repository, reset_hook = (
        _build_repositories(alert_rules=alert_rules)
    )
    artifact_storage = build_artifact_storage()

    pilot_service = PilotService(
        dependencies=PilotService.Dependencies(
            mission_repository=mission_repository,
            alert_repository=alert_repository,
            frame_event_repository=frame_repository,
            artifact_storage=artifact_storage,
        ),
        alert_rules=alert_rules,
    )
    pilot_service.set_report_metadata(report_metadata)

    return AppContainer(
        pilot_service=pilot_service,
        stream_controller=DetectionStreamController(),
        reset_hook=reset_hook,
    )


def get_pilot_service() -> PilotService:
    return get_container().pilot_service


def get_stream_controller() -> DetectionStreamController:
    return get_container().stream_controller


def reset_state() -> None:
    container = get_container()
    container.reset_hook()
    container.pilot_service.reset_runtime_state()
    get_container.cache_clear()


def _build_repositories(
    *,
    alert_rules: AlertRuleConfig,
) -> tuple[
    MissionRepository,
    AlertRepository,
    FrameEventRepository,
    Callable[[], None],
]:
    settings = get_settings()
    backend = settings.app.repository_backend

    if backend == "memory":
        memory_db = InMemoryDatabase()
        return (
            InMemoryMissionRepository(memory_db),
            InMemoryAlertRepository(memory_db),
            InMemoryFrameEventRepository(memory_db),
            lambda: _reset_memory_database(memory_db),
        )

    if backend == "postgres":
        from rescue_ai.infrastructure.postgres_connection import resolve_postgres_dsn
        from rescue_ai.infrastructure.postgres_repositories import (
            EpisodeProjectionSettings,
            PostgresAlertRepository,
            PostgresDatabase,
            PostgresFrameEventRepository,
            PostgresMissionRepository,
        )

        dsn = resolve_postgres_dsn()
        if not dsn:
            raise ValueError(
                "Postgres backend requires APP_POSTGRES_DSN or "
                "APP_POSTGRES_HOST/PORT/DB/USER/PASSWORD"
            )

        postgres_db = PostgresDatabase(dsn=dsn)
        episode_settings = EpisodeProjectionSettings(
            gt_gap_end_sec=alert_rules.gt_gap_end_sec,
            match_tolerance_sec=alert_rules.match_tolerance_sec,
        )
        return (
            PostgresMissionRepository(postgres_db),
            PostgresAlertRepository(postgres_db, episode_settings=episode_settings),
            PostgresFrameEventRepository(
                postgres_db, episode_settings=episode_settings
            ),
            postgres_db.truncate_all,
        )

    raise ValueError("APP_REPOSITORY_BACKEND must be one of: memory, postgres")


def _reset_memory_database(db: InMemoryDatabase) -> None:
    db.missions.clear()
    db.alerts.clear()
    db.mission_frames.clear()
