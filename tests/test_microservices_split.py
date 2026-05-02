"""Tests for ADR-0008 §1 microservice split.

Покрывают четыре уровня:
  1) Сервис nav-engine: round-trip reset → step → response.
  2) Сервис detection: round-trip detect.
  3) HTTP-адаптер NavigationEnginePort: говорит с in-process FastAPI
     приложением через httpx.Client(transport=ASGITransport).
  4) HTTP-адаптер DetectorPort: то же самое.

Тесты НЕ требуют сети — всё гоняется через ASGI-транспорт. cv2 + numpy
обязательны (они уже доустановлены для navigation-тестов).
"""

from __future__ import annotations

import base64
from typing import cast

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from rescue_ai.domain.entities import Detection, TrajectoryPoint
from rescue_ai.domain.value_objects import NavMode, TrajectorySource
from rescue_ai.infrastructure.http_detector import HttpDetector
from rescue_ai.infrastructure.http_navigation_engine import HttpNavigationEngine
from rescue_ai.services.detection.app import build_app as build_detection_app
from rescue_ai.services.nav_engine.app import build_app as build_nav_engine_app


def _client_for(app) -> httpx.Client:
    """TestClient наследует httpx.Client → можно передать в адаптер.

    Это sync-путь к ASGI без реального сетевого слоя — ровно то, что
    нужно для unit-тестов на адаптерах портов.
    """
    return cast(httpx.Client, TestClient(app))


# ── Helpers ────────────────────────────────────────────────────────


def _solid_frame(color_bgr=(60, 60, 60)) -> np.ndarray:
    """640x480 BGR кадр."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :] = color_bgr
    return frame


def _to_jpeg_b64(frame: np.ndarray) -> str:
    success, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert success
    return base64.b64encode(buf.tobytes()).decode("ascii")


# ── nav-engine service ────────────────────────────────────────────


def test_nav_engine_service_health_and_session_lifecycle() -> None:
    app = build_nav_engine_app()
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

        # create
        rsp = client.post(
            "/sessions",
            json={
                "mission_id": "m-1",
                "nav_mode": "no_marker",
                "fps": 6.0,
            },
        )
        assert rsp.status_code == 200
        session_id = rsp.json()["session_id"]
        assert session_id

        # step (одного кадра обычно мало для no_marker, чтобы вернуть
        # точку — но ответ должен быть валидным JSON с {"point": null|...}).
        step_rsp = client.post(
            f"/sessions/{session_id}/step",
            json={
                "frame_jpeg_b64": _to_jpeg_b64(_solid_frame()),
                "ts_sec": 0.0,
                "frame_id": 0,
            },
        )
        assert step_rsp.status_code == 200
        body = step_rsp.json()
        assert "point" in body  # либо null, либо TrajectoryPointResponse

        # drop
        drop_rsp = client.delete(f"/sessions/{session_id}")
        assert drop_rsp.status_code == 204


def test_nav_engine_service_returns_404_for_unknown_session() -> None:
    app = build_nav_engine_app()
    with TestClient(app) as client:
        rsp = client.post(
            "/sessions/no-such-id/step",
            json={
                "frame_jpeg_b64": _to_jpeg_b64(_solid_frame()),
                "ts_sec": 0.0,
            },
        )
        assert rsp.status_code == 404


def test_nav_engine_service_rejects_invalid_base64() -> None:
    app = build_nav_engine_app()
    with TestClient(app) as client:
        sid = client.post(
            "/sessions", json={"mission_id": "m-1"}
        ).json()["session_id"]
        rsp = client.post(
            f"/sessions/{sid}/step",
            json={"frame_jpeg_b64": "not_b64!@#", "ts_sec": 0.0},
        )
        assert rsp.status_code == 400


# ── detection service ────────────────────────────────────────────


class _FakeDetector:
    """Stateless detector stub: возвращает фиксированный набор."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, image_uri: object) -> list[Detection]:
        self.calls += 1
        return [
            Detection(
                bbox=(1.0, 2.0, 3.0, 4.0),
                score=0.91,
                label="person",
                model_name="fake",
                explanation="stub",
            )
        ]

    def warmup(self) -> None:
        return None

    def runtime_name(self) -> str:
        return "fake-runtime"


def test_detection_service_returns_detector_output() -> None:
    fake = _FakeDetector()
    app = build_detection_app(detector_factory=lambda: fake)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        runtime = client.get("/runtime").json()
        assert runtime == {"runtime_name": "fake-runtime"}

        rsp = client.post(
            "/detect",
            json={"frame_jpeg_b64": _to_jpeg_b64(_solid_frame())},
        )
        assert rsp.status_code == 200
        body = rsp.json()
        assert body["runtime_name"] == "fake-runtime"
        assert len(body["detections"]) == 1
        det = body["detections"][0]
        assert det["bbox"] == [1.0, 2.0, 3.0, 4.0]
        assert det["score"] == pytest.approx(0.91)
        assert det["label"] == "person"


def test_detection_service_rejects_invalid_base64() -> None:
    app = build_detection_app(detector_factory=lambda: _FakeDetector())
    with TestClient(app) as client:
        rsp = client.post("/detect", json={"frame_jpeg_b64": "###"})
        assert rsp.status_code == 400


# ── HttpDetector adapter ─────────────────────────────────────────


def test_http_detector_round_trip_via_asgi_transport() -> None:
    fake = _FakeDetector()
    app = build_detection_app(detector_factory=lambda: fake)
    # TestClient запускает FastAPI lifespan только внутри `with`,
    # иначе detector_factory не вызовется и /detect отвечает 503.
    with TestClient(app) as client:
        adapter = HttpDetector(
            base_url="http://testserver",
            client=cast(httpx.Client, client),
        )

        adapter.warmup()
        detections = adapter.detect(_solid_frame())
        assert len(detections) == 1
        det = detections[0]
        assert det.bbox == (1.0, 2.0, 3.0, 4.0)
        assert det.label == "person"
        assert det.model_name == "fake"

        assert adapter.runtime_name() == "fake-runtime"


def test_http_detector_supports_jpeg_bytes_directly() -> None:
    fake = _FakeDetector()
    app = build_detection_app(detector_factory=lambda: fake)
    with TestClient(app) as client:
        adapter = HttpDetector(
            base_url="http://testserver",
            client=cast(httpx.Client, client),
        )

        success, buf = cv2.imencode(".jpg", _solid_frame())
        assert success
        adapter.detect(buf.tobytes())  # bytes path
        assert fake.calls == 1


# ── HttpNavigationEngine adapter ─────────────────────────────────


def test_http_navigation_engine_adapter_full_flow() -> None:
    app = build_nav_engine_app()
    client = _client_for(app)
    engine = HttpNavigationEngine(
        base_url="http://testserver",
        mission_id="m-1",
        client=client,
    )
    engine.reset(nav_mode=NavMode.NO_MARKER, fps=6.0)
    point = engine.step(_solid_frame(), ts_sec=0.0, frame_id=0)
    # Один кадр в no_marker может не дать точку — но возврат должен
    # быть валидным (None или TrajectoryPoint), без исключений.
    assert point is None or isinstance(point, TrajectoryPoint)

    engine.close()


def test_http_navigation_engine_step_without_reset_raises() -> None:
    app = build_nav_engine_app()
    client = _client_for(app)
    engine = HttpNavigationEngine(
        base_url="http://testserver", mission_id="m-1", client=client
    )

    with pytest.raises(RuntimeError, match="before reset"):
        engine.step(_solid_frame(), ts_sec=0.0)

    engine.close()


def test_http_navigation_engine_drops_old_session_on_reset() -> None:
    """Повторный reset должен закрыть старую сессию — иначе утечка."""
    app = build_nav_engine_app()
    client = _client_for(app)
    engine = HttpNavigationEngine(
        base_url="http://testserver", mission_id="m-1", client=client
    )
    engine.reset(nav_mode=NavMode.NO_MARKER, fps=6.0)
    first_id = engine._session_id  # type: ignore[attr-defined]
    engine.reset(nav_mode=NavMode.NO_MARKER, fps=6.0)
    second_id = engine._session_id  # type: ignore[attr-defined]
    assert first_id != second_id

    # Старая сессия больше не должна отвечать.
    rsp = client.post(
        f"/sessions/{first_id}/step",
        json={
            "frame_jpeg_b64": _to_jpeg_b64(_solid_frame()),
            "ts_sec": 0.0,
        },
    )
    assert rsp.status_code == 404
    engine.close()


# ── Sanity: возврат TrajectorySource enum преобразуется корректно ──


def test_trajectory_source_round_trips_through_payload() -> None:
    from rescue_ai.infrastructure.http_navigation_engine import (
        _trajectory_point_from_payload,
    )

    point = _trajectory_point_from_payload(
        {
            "mission_id": "m-1",
            "seq": 5,
            "ts_sec": 1.0,
            "x": 0.0,
            "y": 0.0,
            "z": 1.0,
            "source": "optical_flow",
            "frame_id": 12,
        }
    )
    assert point.source == TrajectorySource.OPTICAL_FLOW
    assert point.frame_id == 12
