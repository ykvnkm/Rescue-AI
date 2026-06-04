"""Unit tests for automatic-mode domain entities and invariants (P1.1)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from rescue_ai.domain.entities import AutoDecision, Mission, TrajectoryPoint
from rescue_ai.domain.value_objects import (
    AutoDecisionKind,
    MissionMode,
    NavMode,
    TrajectorySource,
)


def _mission(**overrides: object) -> Mission:
    base: dict[str, Any] = {
        "mission_id": "m-1",
        "source_name": "cam",
        "status": "pending",
        "created_at": "2026-04-20T00:00:00+00:00",
        "total_frames": 0,
        "fps": 30.0,
    }
    base.update(overrides)
    return Mission(**base)


def test_mission_mode_defaults_to_operator_for_backward_compat() -> None:
    mission = _mission()
    assert mission.mode is MissionMode.OPERATOR


def test_mission_mode_can_be_set_to_automatic() -> None:
    mission = _mission(mode=MissionMode.AUTOMATIC)
    assert mission.mode is MissionMode.AUTOMATIC


def test_mission_mode_str_values_match_schema_check() -> None:
    assert str(MissionMode.OPERATOR) == "operator"
    assert str(MissionMode.AUTOMATIC) == "automatic"


def test_nav_mode_values_match_schema_check() -> None:
    assert {m.value for m in NavMode} == {"marker", "no_marker", "auto"}


def test_trajectory_source_values_match_schema_check() -> None:
    assert {s.value for s in TrajectorySource} == {
        "marker",
        "optical_flow",
        "fallback",
    }


def test_auto_decision_kind_values_match_schema_check() -> None:
    assert {k.value for k in AutoDecisionKind} == {
        "alert_created",
        "alert_suppressed",
    }


def test_trajectory_point_is_frozen() -> None:
    point = TrajectoryPoint(
        mission_id="m-1",
        seq=0,
        ts_sec=0.0,
        x=0.0,
        y=0.0,
        z=0.0,
        source=TrajectorySource.MARKER,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(point, "x", 1.0)


def test_trajectory_point_frame_id_optional() -> None:
    point = TrajectoryPoint(
        mission_id="m-1",
        seq=7,
        ts_sec=1.5,
        x=1.0,
        y=2.0,
        z=3.0,
        source=TrajectorySource.OPTICAL_FLOW,
    )
    assert point.frame_id is None


def test_auto_decision_is_frozen() -> None:
    decision = AutoDecision(
        decision_id="d-1",
        mission_id="m-1",
        ts_sec=0.0,
        kind=AutoDecisionKind.ALERT_CREATED,
        reason="quorum met",
        created_at="2026-04-20T00:00:00+00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(decision, "reason", "mutated")


def test_auto_decision_frame_id_optional() -> None:
    decision = AutoDecision(
        decision_id="d-2",
        mission_id="m-1",
        ts_sec=2.0,
        kind=AutoDecisionKind.ALERT_SUPPRESSED,
        reason="cooldown",
        created_at="2026-04-20T00:00:01+00:00",
    )
    assert decision.frame_id is None
