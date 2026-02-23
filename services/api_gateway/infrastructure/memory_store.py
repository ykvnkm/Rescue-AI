"""In-memory storage for mission and alert flow (MVP)."""

from typing import TypedDict
from uuid import uuid4

from libs.core.domain.entities import Alert, Mission


class FrameRecord(TypedDict):
    """Minimal frame metadata used for GT episode reconstruction."""

    frame_id: int
    ts_sec: float
    gt_person_present: bool


MISSIONS: dict[str, Mission] = {}
ALERTS: dict[str, Alert] = {}
FRAMES: dict[str, list[FrameRecord]] = {}
ALERT_SCORE_THRESHOLD = 0.8


def reset_state() -> None:
    MISSIONS.clear()
    ALERTS.clear()
    FRAMES.clear()


def create_mission() -> Mission:
    mission = Mission(mission_id=str(uuid4()), status="created")
    MISSIONS[mission.mission_id] = mission
    FRAMES[mission.mission_id] = []
    return mission


def mission_exists(mission_id: str) -> bool:
    return mission_id in MISSIONS


def add_alert(
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    score: float,
) -> Alert:
    alert = Alert(
        alert_id=str(uuid4()),
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=ts_sec,
        score=score,
        status="queued",
    )
    ALERTS[alert.alert_id] = alert
    return alert


def ingest_frame(
    mission_id: str,
    frame_id: int,
    ts_sec: float,
    score: float,
    gt_person_present: bool | None = None,
) -> Alert | None:
    FRAMES.setdefault(mission_id, []).append(
        {
            "frame_id": frame_id,
            "ts_sec": ts_sec,
            "gt_person_present": bool(gt_person_present),
        }
    )

    if score < ALERT_SCORE_THRESHOLD:
        return None

    return add_alert(
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=ts_sec,
        score=score,
    )


def list_alerts(mission_id: str | None = None) -> list[Alert]:
    alerts = list(ALERTS.values())
    if mission_id is None:
        return alerts
    return [alert for alert in alerts if alert.mission_id == mission_id]


def update_alert_status(
    alert_id: str,
    status: str,
    reviewed_by: str | None,
) -> Alert | None:
    alert = ALERTS.get(alert_id)
    if alert is None:
        return None
    alert.status = status
    alert.reviewed_by = reviewed_by
    return alert


def list_gt_episodes(mission_id: str) -> list[dict[str, float | int]]:
    frames = sorted(
        FRAMES.get(mission_id, []),
        key=lambda item: item["frame_id"],
    )
    episodes: list[dict[str, float | int]] = []

    start_frame: FrameRecord | None = None
    last_true_frame: FrameRecord | None = None

    for frame in frames:
        gt_present = frame["gt_person_present"]

        if gt_present:
            if start_frame is None:
                start_frame = frame
            last_true_frame = frame
            continue

        if start_frame is not None and last_true_frame is not None:
            episodes.append(
                {
                    "start_frame_id": start_frame["frame_id"],
                    "end_frame_id": last_true_frame["frame_id"],
                    "start_sec": start_frame["ts_sec"],
                    "end_sec": last_true_frame["ts_sec"],
                }
            )
            start_frame = None
            last_true_frame = None

    if start_frame is not None and last_true_frame is not None:
        episodes.append(
            {
                "start_frame_id": start_frame["frame_id"],
                "end_frame_id": last_true_frame["frame_id"],
                "start_sec": start_frame["ts_sec"],
                "end_sec": last_true_frame["ts_sec"],
            }
        )

    return episodes
