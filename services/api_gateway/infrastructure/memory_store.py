"""In-memory storage for mission and alert flow (MVP)."""

from typing import TypedDict
from uuid import uuid4

from libs.core.domain.entities import Alert, Mission


class FrameRecord(TypedDict):
    """Minimal frame metadata used for GT episode reconstruction."""

    frame_id: int
    ts_sec: float
    gt_person_present: bool


class MissionAlertState(TypedDict):
    """Per-mission state for alert generation policy."""

    recent_hit_timestamps: list[float]
    last_alert_ts: float | None
    active_alert: bool
    last_target_seen_ts: float | None


MISSIONS: dict[str, Mission] = {}
ALERTS: dict[str, Alert] = {}
FRAMES: dict[str, list[FrameRecord]] = {}
ALERT_POLICY_STATE: dict[str, MissionAlertState] = {}

ALERT_SCORE_THRESHOLD = 0.2
ALERT_WINDOW_SEC = 1.0
ALERT_QUORUM = 3
ALERT_COOLDOWN_SEC = 5.0
ALERT_GAP_END_SEC = 1.0


def reset_state() -> None:
    MISSIONS.clear()
    ALERTS.clear()
    FRAMES.clear()
    ALERT_POLICY_STATE.clear()


def create_mission() -> Mission:
    mission = Mission(mission_id=str(uuid4()), status="created")
    MISSIONS[mission.mission_id] = mission
    FRAMES[mission.mission_id] = []
    ALERT_POLICY_STATE[mission.mission_id] = {
        "recent_hit_timestamps": [],
        "last_alert_ts": None,
        "active_alert": False,
        "last_target_seen_ts": None,
    }
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


def _touch_policy_state(mission_id: str) -> MissionAlertState:
    state = ALERT_POLICY_STATE.setdefault(
        mission_id,
        {
            "recent_hit_timestamps": [],
            "last_alert_ts": None,
            "active_alert": False,
            "last_target_seen_ts": None,
        },
    )
    return state


def _passes_cooldown(last_alert_ts: float | None, current_ts: float) -> bool:
    if last_alert_ts is None:
        return True
    return (current_ts - last_alert_ts) >= ALERT_COOLDOWN_SEC


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

    state = _touch_policy_state(mission_id=mission_id)
    is_hit = score >= ALERT_SCORE_THRESHOLD

    state["recent_hit_timestamps"] = [
        ts for ts in state["recent_hit_timestamps"] if (ts_sec - ts) <= ALERT_WINDOW_SEC
    ]

    if not is_hit:
        if (
            state["active_alert"]
            and state["last_target_seen_ts"] is not None
            and (ts_sec - state["last_target_seen_ts"]) > ALERT_GAP_END_SEC
        ):
            state["active_alert"] = False
        return None

    state["recent_hit_timestamps"].append(ts_sec)
    state["last_target_seen_ts"] = ts_sec

    if state["active_alert"]:
        return None

    hits_in_window = len(state["recent_hit_timestamps"])
    cooldown_ok = _passes_cooldown(
        last_alert_ts=state["last_alert_ts"],
        current_ts=ts_sec,
    )

    if hits_in_window < ALERT_QUORUM or not cooldown_ok:
        return None

    alert = add_alert(
        mission_id=mission_id,
        frame_id=frame_id,
        ts_sec=ts_sec,
        score=score,
    )
    state["last_alert_ts"] = ts_sec
    state["active_alert"] = True
    return alert


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
