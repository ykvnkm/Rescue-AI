"""CLI entrypoint: stream a video source through the automatic-mission pipeline.

Usage::

    python -m rescue_ai.interfaces.cli.auto_run \\
        --source file:///data/flight.mp4 \\
        --config configs/nsu_frames_yolov8n_alert_contract.yaml

The command is a thin composition root for the automatic mode:

* Resolves a ``--source`` URI into a ``VideoFramePort`` adapter
  (``FileVideoSource``, ``FolderFramesSource``, ``RTSPVideoSource``).
* Builds a detector + :class:`NavigationEngine` + postgres-backed
  repositories + S3 artifact storage + matplotlib trajectory-plot
  renderer.
* Calls ``AutoMissionService.start_auto_mission`` → loops frames through
  ``ingest_frame`` → finalises with ``complete_auto_mission`` which
  writes ``trajectory.csv``, ``plots/trajectory.png``, and
  ``report.json`` to S3 under the same ``{ds}/{mission_id}/`` layout as
  operator missions.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from rescue_ai.application.auto_mission_service import AutoMissionService
from rescue_ai.config import get_settings
from rescue_ai.domain.value_objects import NavMode
from rescue_ai.infrastructure.artifact_storage import build_s3_storage
from rescue_ai.infrastructure.contract_loader import load_stream_contract
from rescue_ai.infrastructure.detectors import build_detector
from rescue_ai.infrastructure.postgres_auto_repositories import (
    PostgresAutoDecisionRepository,
    PostgresAutoMissionConfigRepository,
    PostgresTrajectoryRepository,
)
from rescue_ai.infrastructure.postgres_connection import (
    PostgresDatabase,
    wait_for_postgres,
)
from rescue_ai.infrastructure.postgres_repositories import (
    PostgresAlertRepository,
    PostgresFrameEventRepository,
    PostgresMissionRepository,
)
from rescue_ai.infrastructure.trajectory_plot import build_trajectory_plot_renderer
from rescue_ai.infrastructure.video import (
    FileVideoSource,
    FolderFramesSource,
    RTSPVideoSource,
)
from rescue_ai.navigation.engine import NavigationEngine
from rescue_ai.navigation.tuning import NavigationTuning

logger = logging.getLogger(__name__)


def _build_source(
    uri: str,
    fps_override: float | None,
    default_fps: float = 30.0,
) -> tuple[Iterator[tuple[object, float, int]], str, float]:
    """Return ``(frames_iterator, canonical_source_name, source_fps)`` for a URI.

    Accepted schemes:

    * ``rtsp://`` / ``rtsps://`` → :class:`RTSPVideoSource`
    * ``file://`` or a plain filesystem path → :class:`FileVideoSource`
      (or :class:`FolderFramesSource` if the path is a directory)
    * ``folder://`` → :class:`FolderFramesSource`
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme in {"rtsp", "rtsps"}:
        source = RTSPVideoSource(uri, fps_hint=fps_override or default_fps)
        return source.frames(), uri, source.fps

    if scheme == "folder":
        path = Path(parsed.path or parsed.netloc).expanduser()
        source = FolderFramesSource(path, fps=fps_override or default_fps)
        return source.frames(), path.as_posix(), source.fps

    # File path (with or without file:// scheme).
    raw_path = parsed.path if scheme == "file" else uri
    path = Path(raw_path).expanduser()
    if path.is_dir():
        source = FolderFramesSource(path, fps=fps_override or default_fps)
        return source.frames(), path.as_posix(), source.fps
    source = FileVideoSource(path, fps_override=fps_override)
    return source.frames(), path.as_posix(), source.fps


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rescue-ai-auto-run",
        description="Run a video source through the automatic-mode pipeline.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Video source URI (file path, file://, folder://, rtsp://).",
    )
    parser.add_argument(
        "--config",
        required=False,
        default=None,
        help=(
            "Path to an alert-contract YAML. "
            "Defaults to configs/nsu_frames_yolov8n_alert_contract.yaml."
        ),
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Override the FPS reported by the source (optional).",
    )
    parser.add_argument(
        "--nav-mode",
        default=NavMode.AUTO.value,
        choices=[m.value for m in NavMode],
        help="Navigation mode hint (engine still autodetects marker).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Stop after processing N frames (optional).",
    )
    return parser.parse_args(argv)


def _build_service(
    *,
    settings,
    contract,
    source_name: str,
    fps: float,
) -> AutoMissionService:
    dsn = settings.database.dsn.strip()
    if not dsn:
        raise RuntimeError("DB_DSN is required for auto-run")
    wait_for_postgres(dsn, timeout_sec=settings.api.postgres_ready_timeout_sec)
    db = PostgresDatabase(dsn=dsn, schema="app")

    detector = build_detector(contract.inference)
    nav_tuning = NavigationTuning(fps=fps)
    nav_engine = NavigationEngine(mission_id=source_name, config=nav_tuning)

    return AutoMissionService(
        dependencies=AutoMissionService.Dependencies(
            mission_repository=PostgresMissionRepository(db),
            alert_repository=PostgresAlertRepository(db, episode_settings=None),
            frame_event_repository=PostgresFrameEventRepository(
                db, episode_settings=None
            ),
            trajectory_repository=PostgresTrajectoryRepository(db),
            auto_decision_repository=PostgresAutoDecisionRepository(db),
            auto_mission_config_repository=PostgresAutoMissionConfigRepository(db),
            artifact_storage=build_s3_storage(settings.storage),
            detector=detector,
            navigation_engine=nav_engine,
            trajectory_plot_renderer=build_trajectory_plot_renderer(),
        ),
        alert_rules=contract.alert_rules,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    args = _parse_args(argv)

    settings = get_settings()
    contract = load_stream_contract(
        service_version=settings.app.service_version,
        contract_path=Path(args.config) if args.config else None,
    )

    frames, source_name, fps_final = _build_source(
        args.source,
        fps_override=args.fps,
        default_fps=contract.dataset_fps,
    )

    service = _build_service(
        settings=settings,
        contract=contract,
        source_name=source_name,
        fps=fps_final,
    )

    mission = service.start_auto_mission(
        source_name=source_name,
        total_frames=0,
        fps=fps_final,
        nav_mode=NavMode(args.nav_mode),
        detector_name=contract.inference.detector_name,
        config_json={
            "config_name": contract.config_name,
            "config_hash": contract.config_hash,
            "config_path": contract.config_path,
            "nav_mode_hint": args.nav_mode,
            "fps": fps_final,
        },
    )
    mission_id = mission.mission_id
    logger.info(
        "Auto-mission started: mission_id=%s source=%s fps=%.2f",
        mission_id,
        source_name,
        fps_final,
    )

    processed = 0
    last_frame_id: int | None = None
    try:
        for frame_bgr, ts_sec, frame_id in frames:
            service.ingest_frame(
                mission_id=mission_id,
                frame_bgr=frame_bgr,
                ts_sec=ts_sec,
                frame_id=frame_id,
                image_uri=f"source://{source_name}/{frame_id}",
            )
            processed += 1
            last_frame_id = frame_id
            if processed % 50 == 0:
                logger.info(
                    "auto-run progress: mission=%s frames=%d",
                    mission_id,
                    processed,
                )
            if args.max_frames is not None and processed >= args.max_frames:
                break
    except KeyboardInterrupt:
        logger.warning("auto-run interrupted by user; finalising mission")
    finally:
        service.complete_auto_mission(
            mission_id=mission_id,
            completed_frame_id=last_frame_id,
        )
    logger.info(
        "Auto-mission completed: mission=%s frames=%d",
        mission_id,
        processed,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
